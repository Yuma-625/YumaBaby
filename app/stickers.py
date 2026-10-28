"""管理员表情包收藏：把当前/引用/最近上下文里的图存到本地。

命令只认管理员，其他人说同样的话会当普通聊天。
"""

import base64
import hashlib
import logging
import random
import re
from datetime import datetime, timezone
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


def parse_sticker_tags(text: str) -> str:
    match = re.search(r"标签\s*[:：]?\s*(.+)$", text or "")
    if not match:
        return ""
    raw = match.group(1).strip()
    raw = re.split(r"命名", raw)[0]
    tags = []
    for chunk in re.split(r"[,，、/\s]+", raw):
        chunk = chunk.strip("「」『』\"'“”").strip(_PUNCT)
        if chunk and chunk not in tags:
            tags.append(chunk)
    return ",".join(tags[:10])


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
    tags = parse_sticker_tags(text)
    out_dir = Path(settings.STICKERS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _ALLOWED_CTYPES[ctype]

    existing = store.get_sticker_by_hash(digest)
    if name:
        conflict = store.get_sticker_by_name(name)
        if conflict is not None and (
            existing is None or int(conflict["id"]) != int(existing["id"])
        ):
            taken = Path(conflict["path"]).stem or conflict["name"]
            return (
                f"「{name}」这个名字已经被 #{conflict['id']} 用了（{taken}），"
                "换一个名字吧，比如加个后缀。"
            )

    if existing is not None:
        old_path = Path(existing["path"])
        final_name = name or existing.get("name") or ""
        base = _safe_name(final_name) or digest[:16]
        target = _unique_path(out_dir, base, ext, digest)
        if old_path.exists() and old_path.resolve() != target.resolve():
            try:
                old_path.rename(target)
            except OSError:
                target = old_path
        elif not target.exists():
            target.write_bytes(data)
        store.set_sticker_path_name(existing["id"], str(target), final_name)
        if tags:
            store.set_sticker_tags(int(existing["id"]), tags)
        sticker_id, inserted = int(existing["id"]), False
        renamed = bool(name and name != (existing.get("name") or ""))
        label = final_name or (attachment.get("summary") or "表情").strip("[]")
    else:
        base = _safe_name(name) or digest[:16]
        target = _unique_path(out_dir, base, ext, digest)
        target.write_bytes(data)
        sticker_id, inserted, renamed = store.save_sticker(
            digest,
            str(target),
            ctype=ctype,
            summary=str(attachment.get("summary", "")),
            source_group=group_id or "",
            source_user=user_id,
            name=name,
            tags=tags,
        )
        label = name or (attachment.get("summary") or "表情").strip("[]")

    total = store.count_stickers()
    if inserted:
        suffix = f"「{name}」" if name else f"（{label}）"
        return f"存好了，表情包 #{sticker_id}{suffix}，一共 {total} 个。"
    if renamed:
        return f"已把 #{sticker_id} 命名为「{name}」，一共 {total} 个。"
    suffix = f"「{label}」" if label else ""
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


# ---------------------------------------------------------------- 候选池（收集底子）

def _candidate_dir() -> Path:
    path = Path(settings.STICKER_CANDIDATES_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def collect_candidates(
    store: MemoryStore,
    attachments: list[dict] | None,
    *,
    group_id: str | None,
    user_id: str,
    sender_name: str = "",
    message_id: str = "",
) -> int:
    """把消息里的图片收进候选池；策略由配置决定，默认关闭。"""
    if not settings.STICKER_COLLECT_ENABLED or not attachments:
        return 0
    if settings.STICKER_COLLECT_GROUPS and (group_id or "") not in settings.STICKER_COLLECT_GROUPS:
        return 0
    if settings.STICKER_COLLECT_USERS and user_id not in settings.STICKER_COLLECT_USERS:
        return 0
    if store.count_sticker_candidates("pending") >= settings.STICKER_COLLECT_MAX_PER_DAY:
        logger.info("表情候选达到每日上限，跳过收集")
        return 0

    added = 0
    for attachment in attachments:
        if attachment.get("type") not in _IMAGE_TYPES:
            continue
        url = str(attachment.get("url") or "")
        if not url:
            continue
        downloaded = await _download(url)
        if downloaded is None:
            continue
        data, ctype = downloaded
        if ctype not in _ALLOWED_CTYPES:
            continue
        digest = hashlib.sha1(data).hexdigest()
        if store.get_sticker_by_hash(digest):
            continue
        target = _candidate_dir() / f"{digest[:16]}{_ALLOWED_CTYPES[ctype]}"
        if not target.exists():
            target.write_bytes(data)
        _, inserted = store.add_sticker_candidate(
            digest, str(target), ctype=ctype,
            group_id=group_id or "", user_id=user_id,
            sender_name=sender_name, message_id=message_id,
        )
        added += 1 if inserted else 0
    if added:
        logger.info("收集表情候选: %d 个", added)
    return added


def approve_candidate(store: MemoryStore, candidate_id: int, name: str = "",
                      tags: str = "") -> str:
    candidate = store.get_sticker_candidate(candidate_id)
    if candidate is None:
        return f"没有 #{candidate_id} 这个候选。"
    if candidate.get("status") != "pending":
        return f"#{candidate_id} 已经处理过了。"
    source = Path(candidate["path"])
    if not source.exists():
        return f"候选 #{candidate_id} 的文件不在了。"
    ext = _ALLOWED_CTYPES.get(candidate.get("ctype") or "", source.suffix or ".jpg")
    final_name = _safe_name(name) or f"表情{candidate_id}"
    target = _unique_path(Path(settings.STICKERS_DIR), final_name, ext, candidate["hash"])
    Path(settings.STICKERS_DIR).mkdir(parents=True, exist_ok=True)
    if target.exists():
        source.unlink(missing_ok=True)
    else:
        source.rename(target)
    sticker_id, inserted, _ = store.save_sticker(
        candidate["hash"], str(target), ctype=candidate.get("ctype") or "",
        source_group=candidate.get("group_id") or "",
        source_user=candidate.get("user_id") or "",
        name=final_name,
        tags=tags,
    )
    store.set_sticker_candidate_status(candidate_id, "approved", str(target))
    if inserted:
        return f"收好了，表情包 #{sticker_id}「{final_name}」，一共 {store.count_stickers()} 个。"
    return f"这个之前已经收过，是 #{sticker_id}。"


def reject_candidate(store: MemoryStore, candidate_id: int) -> str:
    candidate = store.get_sticker_candidate(candidate_id)
    if candidate is None:
        return f"没有 #{candidate_id} 这个候选。"
    store.set_sticker_candidate_status(candidate_id, "rejected")
    return f"已拒绝候选 #{candidate_id}。"


def handle_sticker_admin(text: str, store: MemoryStore, user_id: str) -> str | None:
    """管理员候选池命令：表情候选 / 收 3 命名xx / 拒 3。"""
    if user_id != settings.QQ_PERSONA_ADMIN:
        return None
    value = (text or "").strip().strip(_PUNCT)
    if value in ("表情候选", "表情审核", "候选表情"):
        pending = store.list_sticker_candidates("pending", limit=8)
        if not pending:
            return "候选池是空的。"
        lines = [f"待审核表情 {store.count_sticker_candidates('pending')} 个："]
        for item in pending:
            lines.append(
                f"#{item['id']} {item.get('sender_name') or item.get('user_id') or '群友'}"
                f"（{item.get('group_id') or '私聊'}）"
            )
        lines.append("回复「收 1 命名xx」或「拒 1」。")
        return "\n".join(lines)

    match = re.match(r"^(?:收|通过|采纳)\s*#?(\d+)(.*)$", value)
    if match:
        tail = match.group(2) or ""
        return approve_candidate(
            store, int(match.group(1)),
            parse_sticker_name(tail), parse_sticker_tags(tail),
        )
    match = re.match(r"^(?:拒|拒绝|不要)\s*#?(\d+)$", value)
    if match:
        return reject_candidate(store, int(match.group(1)))

    if value in ("表情列表", "表情库"):
        stickers = store.list_stickers(limit=20)
        if not stickers:
            return "表情库是空的。"
        lines = [f"表情库 {store.count_stickers()} 个："]
        for item in sorted(stickers, key=lambda s: s["id"]):
            tags = item.get("tags") or "无标签"
            lines.append(
                f"#{item['id']} {item.get('name') or '未命名'} | {tags} | 用过 {item.get('use_count') or 0} 次"
            )
        return "\n".join(lines)

    match = re.match(r"^表情标签\s*#?(\d+)\s+(.+)$", value)
    if match:
        tags = parse_sticker_tags("标签 " + match.group(2)) or match.group(2).strip()
        if not store.set_sticker_tags(int(match.group(1)), tags):
            return f"没有 #{match.group(1)} 这个表情。"
        return f"已给 #{match.group(1)} 打标签：{tags}"
    return None


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

_BAD_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name: str) -> str:
    return _BAD_NAME_CHARS.sub("_", (name or "").strip()).strip(" .")[:40]


def _unique_path(directory: Path, base: str, ext: str, digest: str) -> Path:
    candidate = directory / f"{base}{ext}"
    if not candidate.exists():
        return candidate
    try:
        if hashlib.sha1(candidate.read_bytes()).hexdigest() == digest:
            return candidate
    except OSError:
        pass
    return directory / f"{base}-{digest[:6]}{ext}"


_HASH_STEM = re.compile(r"^[0-9a-f]{16}$")


def sync_sticker_files(store: MemoryStore) -> int:
    """扫描表情目录：文件被手动改名/移动后，按内容 hash 修正数据库。

    - 已有记录：更新 path；有名字时把文件改名成该名字
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
        row = store.get_sticker_by_hash(digest)

        if row is None:
            name = "" if _HASH_STEM.match(file.stem) else file.stem
            store.save_sticker(
                digest, str(file), ctype=_EXT_CTYPE[file.suffix.lower()],
                summary="", name=name,
            )
            changed += 1
            continue

        name = row["name"] or ("" if _HASH_STEM.match(file.stem) else file.stem)
        target = file
        if row["name"] and file.stem != row["name"]:
            candidate = _unique_path(
                directory, _safe_name(row["name"]) or digest[:16], file.suffix.lower(), digest
            )
            if candidate != file and not candidate.exists():
                try:
                    file.rename(candidate)
                    target = candidate
                except OSError:
                    target = file
        store.set_sticker_path_name(row["id"], str(target), name)
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
    """发表情：所有人可用（保存仍限管理员）。"""
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
            store.mark_sticker_used(int(sticker["id"]))
            logger.info(f"发送表情 #{sticker['id']}")
    if not messages:
        return "这些表情文件不在了或太大，发不出来。"
    return "".join(messages)


# ---------------------------------------------------------------- 回复语气选表情

_TONE_RULES = (
    ("无语", ("无语", "服了", "离谱", "真会", "懒得", "算了", "汗", "拿你没办法", "沉默", "……")),
    ("开心", ("哈哈", "笑死", "开心", "好耶", "嘿嘿", "乐")),
    ("傲娇", ("哼", "才不", "略", "笨蛋", "不要")),
    ("得意", ("牛", "厉害", "不错", "可以啊", "当然", "包的")),
    ("疑惑", ("？", "?", "什么", "真的假的", "啊")),
    ("丧", ("唉", "难过", "惨", "寄", "累")),
)


def _tone_preferences() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for chunk in (settings.STICKER_TONE_MAP or "").replace("，", ",").split(","):
        if ":" not in chunk:
            continue
        tone, name = chunk.split(":", 1)
        tone, name = tone.strip(), name.strip()
        if tone and name:
            mapping[tone] = name
    return mapping


def classify_tone(text: str) -> set[str]:
    """轻量语气分类：不额外调模型，先覆盖常见口癖。"""
    value = text or ""
    tones: set[str] = set()
    for tone, words in _TONE_RULES:
        if any(word in value for word in words):
            tones.add(tone)
    return tones


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None


def _sticker_tags(sticker: dict) -> set[str]:
    raw = str(sticker.get("tags") or "")
    return {item.strip() for item in re.split(r"[,，、/\s]+", raw) if item.strip()}


def maybe_reply_sticker(store: MemoryStore, reply: str, *,
                        group_id: str) -> str:
    """reply 模式：按回复语气匹配标签，命中才发；默认关闭。"""
    mode = settings.STICKER_SEND_MODE
    if mode not in ("reply", "both", "random"):
        return ""
    if not reply or "[CQ:image" in reply:
        return ""
    if group_id in settings.QQ_READONLY_GROUPS:
        return ""
    if settings.STICKER_SEND_GROUPS and group_id not in settings.STICKER_SEND_GROUPS:
        return ""
    if len(reply.strip()) < settings.STICKER_SEND_MIN_CHARS:
        return ""
    if settings.STICKER_SEND_CHANCE <= 0:
        return ""
    if random.random() >= settings.STICKER_SEND_CHANCE:
        return ""

    chat_id = f"group:{group_id}"
    if settings.STICKER_SEND_COOLDOWN_SECONDS > 0:
        age = _age_seconds(store.last_event_at("sticker_send", chat_id))
        if age is not None and age < settings.STICKER_SEND_COOLDOWN_SECONDS:
            return ""
    if store.count_events_today("sticker_send", chat_id) >= settings.STICKER_SEND_DAILY_LIMIT:
        return ""

    tones = classify_tone(reply)
    if not tones:
        return ""
    candidates = [
        sticker for sticker in store.list_stickers(limit=200)
        if _sticker_tags(sticker) & tones
    ]
    if not candidates:
        return ""

    # 指定语气优先用指定表情，例如“无语”固定发大狗无语
    preferred = None
    preferences = _tone_preferences()
    for tone in tones:
        name = preferences.get(tone)
        if not name:
            continue
        preferred = next(
            (s for s in candidates if (s.get("name") or "") == name), None
        )
        if preferred is not None:
            break

    if preferred is not None:
        sticker = preferred
    else:
        weights = []
        for sticker in candidates:
            age = _age_seconds(sticker.get("last_used_at"))
            recency = 1.0
            if age is not None and age < 3600:
                recency = 0.2
            elif age is not None and age < 86400:
                recency = 0.6
            weights.append(recency / (1.0 + int(sticker.get("use_count") or 0)))
        sticker = random.choices(candidates, weights=weights, k=1)[0]
    cq = _build_image_cq(sticker)
    if not cq:
        return ""
    store.mark_sticker_used(int(sticker["id"]))
    store.add_event(
        "sticker_send",
        name=str(sticker.get("name") or f"#{sticker['id']}"),
        chat_id=chat_id,
        detail={"sticker_id": sticker["id"], "tones": sorted(tones), "mode": mode},
    )
    logger.info("按语气发表情 #%s tones=%s", sticker["id"], sorted(tones))
    return cq

