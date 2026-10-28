"""把 QQ 图片附件转成 OpenAI 兼容的 image_url 内容段。

- 只处理 image 类型；表情包（mface）仍按文字占位
- 图片 URL 走 SSRF 校验，下载有大小上限
- 只给当前这一轮用，不写进历史，避免每轮重复计费
"""

import base64
import logging

import httpx

from app.config import settings
from app.tools.builtin import check_url
from app.utils.media import detect_image_mime

logger = logging.getLogger(__name__)

_IMAGE_TYPES = {"image", "mface", "bface", "marketface"}
_ALLOWED_CTYPES = {"image/png", "image/jpeg", "image/webp"}


async def attachment_to_part(attachment: dict) -> dict | None:
    url = str(attachment.get("url") or "").strip()
    if not url:
        return None

    if url.startswith("data:image/"):
        header, _, payload = url.partition(",")
        ctype = header[5:].split(";")[0].lower()
        try:
            data = base64.b64decode(payload)
        except Exception:
            # 兼容测试/异常 data URI：类型合法就原样透传
            if ctype in _ALLOWED_CTYPES:
                return {"type": "image_url", "image_url": {"url": url}}
            return None
        detected = detect_image_mime(data)
        if detected:
            ctype = detected
        if ctype not in _ALLOWED_CTYPES:
            logger.debug(f"跳过不支持的图片类型: {ctype}")
            return None
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{ctype};base64,{base64.b64encode(data).decode()}"
            },
        }

    reason = check_url(url)
    if reason:
        logger.warning(f"拒绝加载图片: {reason}")
        return None

    try:
        async with httpx.AsyncClient(timeout=settings.VISION_TIMEOUT, follow_redirects=True) as client:
            async with client.stream(
                "GET", url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://q.qq.com/"},
            ) as resp:
                if resp.status_code != 200:
                    logger.warning(f"图片下载失败 HTTP {resp.status_code}")
                    return None
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()

                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > settings.VISION_MAX_BYTES:
                        logger.warning("图片超过大小上限，跳过")
                        return None
                    chunks.append(chunk)
                data = b"".join(chunks)
    except Exception as e:
        logger.warning(f"图片下载异常: {type(e).__name__}: {e}")
        return None

    detected = detect_image_mime(data)
    if detected:
        ctype = detected
    if ctype not in _ALLOWED_CTYPES:
        logger.warning(f"跳过不支持的图片类型: {ctype or '未知'}")
        return None
    encoded = base64.b64encode(data).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{ctype};base64,{encoded}"},
    }


async def build_image_parts(attachments: list[dict] | None,
                            limit: int | None = None) -> list[dict]:
    if not attachments or not settings.VISION_ENABLED:
        return []
    limit = max(0, limit if limit is not None else settings.VISION_MAX_IMAGES)
    parts: list[dict] = []
    for attachment in attachments:
        if len(parts) >= limit:
            break
        if attachment.get("type") not in _IMAGE_TYPES:
            continue
        part = await attachment_to_part(attachment)
        if part:
            parts.append(part)
    return parts
