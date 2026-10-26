"""管理员表情包收藏：把当前/引用/最近上下文里的图存到本地。

命令只认管理员，其他人说同样的话会当普通聊天。
"""

import base64
import hashlib
import logging
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
) -> str:
    attachment = _pick_attachment(attachments) or _pick_from_context(store, group_id)
    if attachment is None:
        return "没找到要存的表情包。先把表情发出来，或者引用它再说「存这个表情包」。"

    downloaded = await _download(str(attachment.get("url", "")))
    if downloaded is None:
        return "这个表情我下载不下来（可能过期或格式不支持），重新发一次再试。"

    data, ctype = downloaded
    digest = hashlib.sha1(data).hexdigest()
    sticker_id, inserted = store.save_sticker(
        digest,
        str(Path(settings.STICKERS_DIR) / f"{digest[:16]}{_ALLOWED_CTYPES[ctype]}"),
        ctype=ctype,
        summary=str(attachment.get("summary", "")),
        source_group=group_id or "",
        source_user=user_id,
    )

    out_dir = Path(settings.STICKERS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{digest[:16]}{_ALLOWED_CTYPES[ctype]}"
    if not path.exists():
        path.write_bytes(data)

    total = store.count_stickers()
    label = (attachment.get("summary") or "表情").strip("[]")
    if inserted:
        return f"存好了，表情包 #{sticker_id}（{label}），一共 {total} 个。"
    return f"这个之前存过了，是 #{sticker_id}（{label}），一共 {total} 个。"


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
        store, attachments=attachments, group_id=group_id, user_id=user_id
    )
