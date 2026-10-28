"""管理员群配置命令：加群 / 拒绝 / 群列表，运行时生效，不用重启。"""

import logging
import re

from app.config import settings
from app.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_OPENID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,80}$")
_SCAN_WORDS = (
    "扫新群", "扫描新群", "扫群", "扫描群",
    "扫一下最近新加的群", "扫一下新群", "扫描最近新加的群",
)


def is_scan_group_command(text: str) -> bool:
    value = (text or "").strip()
    return value in _SCAN_WORDS


def _parse_mode(args: str) -> tuple[str, bool]:
    mode = "normal"
    participation = False
    for token in args.replace("，", " ").split():
        if token in ("只读", "readonly", "read"):
            mode = "readonly"
        elif token in ("正常", "normal", "普通"):
            mode = "normal"
        elif token in ("旁听", "participate", "auto"):
            participation = True
    if mode == "readonly":
        participation = False
    return mode, participation


def handle_group_admin(text: str, store: MemoryStore, user_id: str) -> str | None:
    if user_id != settings.QQ_PERSONA_ADMIN:
        return None
    value = (text or "").strip()

    if value in ("群列表", "群配置", "群管理"):
        lines = ["群配置（数据库优先，其次 .env）："]
        runtime = store.list_runtime_groups()
        for item in runtime:
            mode = "只读" if item["mode"] == "readonly" else "正常"
            extra = " +旁听" if item["participation"] else ""
            lines.append(f"  {item['group_id']}  {mode}{extra}（数据库）")
        for gid in settings.QQ_TARGET_GROUPS:
            if store.get_runtime_group(gid):
                continue
            mode = "只读" if gid in settings.QQ_READONLY_GROUPS else "正常"
            extra = " +旁听" if gid in settings.QQ_PARTICIPATION_GROUPS else ""
            lines.append(f"  {gid}  {mode}{extra}（.env）")
        if len(lines) == 1:
            lines.append("  还没有配置任何群。")
        return "\n".join(lines)

    if value in ("待审群", "新群", "未配置群"):
        pending = store.list_unknown_groups("pending")
        if not pending:
            return "没有待处理的新群。"
        lines = ["待审新群："]
        for item in pending:
            lines.append(
                f"  {item['group_id']}  首次 @ {item.get('sender_name') or item.get('user_id')}"
                f"：{(item.get('sample') or '')[:40]}"
            )
        return "\n".join(lines)

    match = re.match(r"^(?:加群|添加群)\s+(\S+)(?:\s+(.*))?$", value)
    if match:
        group_id = match.group(1)
        if not _OPENID_RE.match(group_id):
            return "openid 格式不对，应该是字母数字下划线组合。"
        mode, participation = _parse_mode(match.group(2) or "")
        store.upsert_runtime_group(group_id, mode=mode, participation=participation)
        if store.get_unknown_group(group_id):
            store.set_unknown_group_status(group_id, "added")
        label = "只读" if mode == "readonly" else "正常"
        extra = "，允许旁听接话" if participation else ""
        return f"已加群 {group_id}：{label}{extra}。下一条消息立即生效，不用重启。"

    match = re.match(r"^(?:拒绝|忽略|不加)\s+(\S+)$", value)
    if match:
        group_id = match.group(1)
        if not _OPENID_RE.match(group_id):
            return "openid 格式不对。"
        store.set_unknown_group_status(group_id, "rejected")
        return f"已拒绝群 {group_id}，以后不再提示。"

    match = re.match(r"^(?:删群|移除群)\s+(\S+)$", value)
    if match:
        removed = store.delete_runtime_group(match.group(1))
        return "已从运行时配置移除。" if removed else "数据库里没有这个群。"

    return None
