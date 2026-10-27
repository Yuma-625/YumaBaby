"""沙箱文件工具：只能读写 SANDBOX_ROOT 下的文件。

仅注册给管理员私聊使用（由 AgentService 过滤工具白名单）。
这不是 OS 级安全隔离，只是路径 jail + 大小限制 + 审计。
"""

import logging
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.memory.store import MemoryStore
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

TOOL_NAMES = {"fs_list", "fs_read", "fs_write", "fs_edit", "fs_glob", "fs_grep"}
_MAX_ENTRIES = 200
_MAX_MATCHES = 100
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _root() -> Path:
    root = Path(settings.SANDBOX_ROOT).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _resolve(relative: str) -> Path:
    raw = (relative or ".").strip()
    if "\x00" in raw or _DRIVE_RE.match(raw) or raw.startswith("\\\\"):
        raise ValueError("只允许沙箱内的相对路径")
    candidate = ( _root() / raw ).resolve()
    root = _root()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("路径越界：只能访问沙箱目录")
    if candidate.exists() and candidate.is_symlink():
        raise ValueError("不允许访问符号链接")
    return candidate


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def _audit(store: MemoryStore | None, tool: str, detail: dict[str, Any], ok: bool = True) -> None:
    if store is None:
        return
    try:
        from app.tools.builtin import current_scope

        scope = current_scope()
        chat_id = f"{scope[0]}:{scope[1]}" if scope else ""
        store.add_event("sandbox", tool, chat_id, detail, ok=ok)
    except Exception:
        logger.debug("沙箱审计失败", exc_info=True)


def list_dir(relative: str = ".") -> dict:
    target = _resolve(relative)
    if not target.exists():
        raise ValueError("目录不存在")
    if not target.is_dir():
        raise ValueError("不是目录")
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if len(entries) >= _MAX_ENTRIES:
            break
        try:
            size = item.stat().st_size if item.is_file() else 0
        except OSError:
            size = 0
        entries.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "size": size,
        })
    return {"path": target.relative_to(_root()).as_posix(), "entries": entries}


def read_file(relative: str, max_chars: int = 20000) -> dict:
    target = _resolve(relative)
    if not target.exists() or not target.is_file():
        raise ValueError("文件不存在")
    size = target.stat().st_size
    if size > settings.SANDBOX_MAX_READ_BYTES:
        raise ValueError(f"文件太大（>{settings.SANDBOX_MAX_READ_BYTES} 字节）")
    data = target.read_bytes()
    if _is_binary(data):
        raise ValueError("二进制文件不支持读取")
    text = data.decode("utf-8", errors="replace")
    max_chars = max(100, min(int(max_chars), 50000))
    return {
        "path": target.relative_to(_root()).as_posix(),
        "size": size,
        "content": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


def write_file(relative: str, content: str, overwrite: bool = False) -> dict:
    target = _resolve(relative)
    if target.exists() and not overwrite:
        raise ValueError("文件已存在；需要覆盖请传 overwrite=true")
    encoded = (content or "").encode("utf-8")
    if len(encoded) > settings.SANDBOX_MAX_WRITE_BYTES:
        raise ValueError(f"内容太大（>{settings.SANDBOX_MAX_WRITE_BYTES} 字节）")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    return {"path": target.relative_to(_root()).as_posix(), "bytes": len(encoded)}


def edit_file(relative: str, old: str, new: str, replace_all: bool = False) -> dict:
    target = _resolve(relative)
    if not target.exists() or not target.is_file():
        raise ValueError("文件不存在")
    data = target.read_bytes()
    if _is_binary(data):
        raise ValueError("二进制文件不支持编辑")
    text = data.decode("utf-8", errors="replace")
    if not old:
        raise ValueError("old 不能为空")
    if old not in text:
        raise ValueError("文件里没有找到要替换的文本")
    updated = text.replace(old, new, -1 if replace_all else 1)
    encoded = updated.encode("utf-8")
    if len(encoded) > settings.SANDBOX_MAX_WRITE_BYTES:
        raise ValueError(f"结果太大（>{settings.SANDBOX_MAX_WRITE_BYTES} 字节）")
    target.write_bytes(encoded)
    return {
        "path": target.relative_to(_root()).as_posix(),
        "replaced": text.count(old) if replace_all else 1,
        "bytes": len(encoded),
    }


def glob_files(pattern: str, limit: int = 100) -> dict:
    raw = (pattern or "*").strip()
    if _DRIVE_RE.match(raw) or raw.startswith("\\\\") or ".." in Path(raw).parts:
        raise ValueError("pattern 必须是沙箱内的相对匹配")
    root = _root()
    limit = max(1, min(int(limit), _MAX_ENTRIES))
    matches = []
    for item in root.glob(raw):
        try:
            item.relative_to(root)
        except ValueError:
            continue
        if item.is_file():
            matches.append(item.relative_to(root).as_posix())
        if len(matches) >= limit:
            break
    return {"pattern": raw, "matches": sorted(matches)}


def grep_files(pattern: str, glob: str = "*", limit: int = 50) -> dict:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"正则不合法: {e}")
    raw_glob = (glob or "*").strip()
    if _DRIVE_RE.match(raw_glob) or raw_glob.startswith("\\\\") or ".." in Path(raw_glob).parts:
        raise ValueError("glob 必须是沙箱内的相对匹配")
    root = _root()
    limit = max(1, min(int(limit), _MAX_MATCHES))
    matches = []
    scanned = 0
    for item in root.glob(raw_glob):
        if not item.is_file():
            continue
        scanned += 1
        if scanned > _MAX_ENTRIES:
            break
        try:
            if item.stat().st_size > settings.SANDBOX_MAX_READ_BYTES:
                continue
            data = item.read_bytes()
        except OSError:
            continue
        if _is_binary(data):
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line):
                matches.append({
                    "path": item.relative_to(root).as_posix(),
                    "line": number,
                    "text": line[:200],
                })
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}


def register_sandbox_tools(registry: ToolRegistry, store: MemoryStore) -> None:
    _root()  # 确保目录存在

    def audited(tool: str, handler):
        def wrapper(**kwargs):
            result = handler(**kwargs)
            _audit(store, tool, {"path": kwargs.get("path") or kwargs.get("relative")})
            return result
        return wrapper

    registry.add(
        "fs_list",
        "列出沙箱目录下的文件。路径必须是相对路径。",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对路径，默认 ."}},
            "required": [],
        },
        audited("fs_list", list_dir),
    )
    registry.add(
        "fs_read",
        "读取沙箱内的文本文件。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "max_chars": {"type": "integer", "description": "最多返回多少字符"},
            },
            "required": ["path"],
        },
        audited("fs_read", read_file),
    )
    registry.add(
        "fs_write",
        "在沙箱内创建或覆盖文本文件。覆盖必须显式传 overwrite=true。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "content": {"type": "string", "description": "文件内容"},
                "overwrite": {"type": "boolean", "description": "是否允许覆盖"},
            },
            "required": ["path", "content"],
        },
        audited("fs_write", write_file),
    )
    registry.add(
        "fs_edit",
        "在沙箱内编辑文本文件：把 old 替换成 new，默认只替换一次。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "old": {"type": "string", "description": "要替换的原文"},
                "new": {"type": "string", "description": "替换后的文本"},
                "replace_all": {"type": "boolean", "description": "是否全部替换"},
            },
            "required": ["path", "old", "new"],
        },
        audited("fs_edit", edit_file),
    )
    registry.add(
        "fs_glob",
        "按 glob 模式查找沙箱内文件，例如 **/*.py。",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式"},
                "limit": {"type": "integer", "description": "最多返回数量"},
            },
            "required": ["pattern"],
        },
        glob_files,
    )
    registry.add(
        "fs_grep",
        "在沙箱内按正则搜索文本，返回文件、行号和匹配行。",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "glob": {"type": "string", "description": "限定文件，默认 *"},
                "limit": {"type": "integer", "description": "最多匹配数"},
            },
            "required": ["pattern"],
        },
        grep_files,
    )
