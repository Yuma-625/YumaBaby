"""管理员表情包收藏：把当前/引用/最近上下文里的图存到本地。

命令只认管理员，其他人说同样的话会当普通聊天。
"""

import base64
import hashlib
import logging
import random
import re
from pathlib import Path

import httpx

from app.config import settings
from app.memory.store import MemoryStore
from app.tools.builtin import check_url

logger = logging.getLogger(__name__)

_IMAGE_TYPES = {"image", "mface", "bface", "marketface"}
_ALLOWED_CTYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_PUNCT = "。！!？?~～…、,，.  "
_NAME_RE = re.compile(r"命名\s*[:：]?\s*[「『\"'“]?([^\s，。！？!?~～「」『』\"'“”]{1,20})")


def parse_sticker_name(text: str) -> str:
    match = _NAME_RE.search(text or "")
    if not match:
        return ""
    return match.group(1).strip("「」『』\"'“”")


def is_sticker_command(text: str) -> bool:
    value = (text or "").strip().strip(_PUNCT)
    if "表情" not in value or len(value) > 30:
        return False
    return any(word in value for word in ("保存", "收藏", "存下", "存这个", "存一下"))


def _pick_attachment(attachments: list[dict] | None) -> dict | None:
    for attachment in reversed(attachments or []):
        if attachment.get("type") in _IMAGE_TYPES and attachment.get("url"):
            return attachment
    return None


def _pick_from_context(store: MemoryStore, group_id: str | None) -> dict | None:
    if not group_id:
        return None
    entries = store.recent_group_context(
        group_id, limit=10, minutes=settings.STICKER_LOOKBACK_MINUTES
    )
    for entry in reversed(entries):
        found = _pick_attachment(entry.get("attachments"))
        if found:
            return found
    return None


async def _download(url: str) -> tuple[bytes, str] | None:
    if url.startswith("data:image/"):
        header, _, payload = url.partition(",")
        ctype = header[5:].split(";")[0].lower()
        if ctype not in _ALLOWED_CTYPES:
            return None
        try:
            data = base64.b64decode(payload)
        except Exception:
            return None
        return (data, ctype) if len(data) <= settings.STICKER_MAX_BYTES else None

    reason = check_url(url)
    if reason:
        logger.warning(f"拒绝下载表情: {reason}")
        return None

    try:
        async with httpx.AsyncClient(timeout=settings.VISION_TIMEOUT, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status_code != 200:
                    return None
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype not in _ALLOWED_CTYPES:
                    logger.warning(f"表情类型不支持: {ctype}")
                    return None
                chunks = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > settings.STICKER_MAX_BYTES:
                        logger.warning("表情超过大小上限")
                        return None
                    chunks.append(chunk)
                return b"".join(chunks), ctype
    except Exception as e:
        logger.warning(f"表情下载失败: {type(e).__name__}: {e}")
        return None


async def save_sticker(
    store: MemoryStore,
    *,
    attachments: list[dict] | None,
    group_id: str | None,
    user_id: str,
    text: str = "",
) -> str:
    attachment = _pick_attachment(attachments) or _pick_from_context(store, group_id)
    if attachment is None:
        return "没找到要存的表情包。先把表情发出来，或者引用它再说「存这个表情包」。"

    downloaded = await _download(str(attachment.get("url", "")))
    if downloaded is None:
        return "这个表情我下载不下来（可能过期或格式不支持），重新发一次再试。"

    data, ctype = downloaded
    digest = hashlib.sha1(data).hexdigest()
    name = parse_sticker_name(text)
    sticker_id, inserted, renamed = store.save_sticker(
        digest,
        str(Path(settings.STICKERS_DIR) / f"{digest[:16]}{_ALLOWED_CTYPES[ctype]}"),
        ctype=ctype,
        summary=str(attachment.get("summary", "")),
        source_group=group_id or "",
        source_user=user_id,
        name=name,
    )

    out_dir = Path(settings.STICKERS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{digest[:16]}{_ALLOWED_CTYPES[ctype]}"
    if not path.exists():
        path.write_bytes(data)

    total = store.count_stickers()
    label = name or (attachment.get("summary") or "表情").strip("[]")
    if inserted:
        suffix = f"「{name}」" if name else f"（{label}）"
        return f"存好了，表情包 #{sticker_id}{suffix}，一共 {total} 个。"
    if renamed:
        return f"已把 #{sticker_id} 命名为「{name}」，一共 {total} 个。"
    suffix = f"「{name}」" if name else f"（{label}）"
    return f"这个之前存过了，是 #{sticker_id}{suffix}，一共 {total} 个。"


async def handle_sticker_command(
    text: str,
    *,
    store: MemoryStore,
    attachments: list[dict] | None,
    group_id: str | None,
    user_id: str,
) -> str | None:
    if not is_sticker_command(text):
        return None
    if user_id != settings.QQ_PERSONA_ADMIN:
        return None
    return await save_sticker(
        store, attachments=attachments, group_id=group_id, user_id=user_id,
        text=text,
    )


# --------------------------------------------------------------------- 发表情

_SEND_WORDS = ("发", "来")
_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_COUNT_RE = re.compile(r"(\d+|[一二两三四五六七八九十])\s*(?:个|张)")
_ID_RE = re.compile(r"(\d+)\s*号")
_ALL_WORDS = ("全部", "所有", "都", "每张", "每个")
_SEND_PREFIX = re.compile(r"^(发送|发个|发|来|给我发|给我来|把)")
MAX_SEND_IMAGES = 5


def is_send_command(text: str) -> bool:
    value = (text or "").strip().strip(_PUNCT)
    if "表情" not in value or len(value) > 40:
        return False
    if is_sticker_command(value):
        return False
    return any(word in value for word in _SEND_WORDS)


def parse_send_ids(text: str) -> list[int]:
    """显式指定的编号：`3号` / `发表情 3`。"""
    value = text or ""
    ids = [int(n) for n in _ID_RE.findall(value)]
    if not ids:
        for match in re.finditer(r"\d+", value):
            number = match.group(0)
            after = value[match.end():match.end() + 2]
            if not after.lstrip().startswith(("个", "张")):
                ids.append(int(number))
    return [n for n in ids if 1 <= n <= 999]


def parse_send_id(text: str) -> int | None:
    ids = parse_send_ids(text)
    return ids[0] if ids else None


def parse_send_count(text: str) -> int | None:
    match = _COUNT_RE.search(text or "")
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    return _CN_DIGITS.get(token)


def _build_image_cq(sticker: dict) -> str | None:
    path = Path(sticker.get("path", ""))
    if not path.exists() or path.stat().st_size > settings.STICKER_MAX_BYTES:
        return None
    data = path.read_bytes()
    return "[CQ:image,file=base64://" + base64.b64encode(data).decode() + "]"


_EXT_CTYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def sync_sticker_files(store: MemoryStore) -> int:
    """扫描表情目录：文件被手动改名/移动后，按内容 hash 修正数据库。

    - 已有记录：更新 path；名字为空时用文件名补上
    - 没有记录的新文件：直接收录，名字取文件名
    """
    directory = Path(settings.STICKERS_DIR)
    if not directory.exists():
        return 0

    changed = 0
    for file in directory.iterdir():
        if not file.is_file() or file.suffix.lower() not in _EXT_CTYPE:
            continue
        try:
            data = file.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha1(data).hexdigest()
        if store.update_sticker(digest, str(file), name=file.stem) is None:
            store.save_sticker(
                digest, str(file), ctype=_EXT_CTYPE[file.suffix.lower()],
                summary="", name=file.stem,
            )
        changed += 1
    return changed


def _files_missing(store: MemoryStore) -> bool:
    return any(not Path(row["path"]).exists() for row in store.list_stickers(200))


async def handle_sticker_send(
    text: str,
    *,
    store: MemoryStore,
    user_id: str,
) -> str | None:
    """管理员发表情：`发 3 号表情` / `发送大狗无语` / `发出来 3 个` / 随机。"""
    if user_id != settings.QQ_PERSONA_ADMIN:
        return None

    if _files_missing(store):
        logger.info("检测到表情文件缺失或改名，执行同步")
        sync_sticker_files(store)

    total = store.count_stickers()
    if total == 0:
        return "表情库还是空的，先说「存这个表情包」存一个。" if is_send_command(text) else None

    value = (text or "").strip().strip(_PUNCT)
    all_stickers = sorted(store.list_stickers(limit=200), key=lambda s: s["id"])
    named = next(
        (s for s in sorted(all_stickers, key=lambda s: len(s.get("name") or ""), reverse=True)
         if s.get("name") and s["name"] in value),
        None,
    )

    # 没有「表情」二字，但以发/发送/来开头且点名了已存表情，也认
    if not is_send_command(text):
        if not (named and _SEND_PREFIX.match(value)):
            return None

    ids = parse_send_ids(text)
    count = parse_send_count(text)
    wants_all = any(word in text for word in _ALL_WORDS)

    if ids:
        missing = [n for n in ids if store.get_sticker(n) is None]
        chosen = [store.get_sticker(n) for n in ids if store.get_sticker(n) is not None]
        if not chosen:
            return f"没有 {'、'.join(f'#{n}' for n in missing)} 这个表情，当前一共 {total} 个。"
        if missing:
            logger.info(f"跳过的表情编号: {missing}")
    elif count:
        chosen = all_stickers[:max(1, count)]
    elif wants_all:
        chosen = all_stickers
    elif named:
        chosen = [named]
    else:
        chosen = [random.choice(all_stickers)]

    chosen = chosen[:MAX_SEND_IMAGES]
    messages = []
    for sticker in chosen:
        cq = _build_image_cq(sticker)
        if cq:
            messages.append(cq)
            logger.info(f"发送表情 #{sticker['id']}")
    if not messages:
        return "这些表情文件不在了或太大，发不出来。"
    return "".join(messages)

