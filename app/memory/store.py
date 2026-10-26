"""短期对话、长期记忆、行为日志与回复评价，统一落在 SQLite。

表：

- ``messages``       与机器人交互的消息（收/发），含平台 message_id
- ``memories``       按 scope（user/group）隔离的事实
- ``events``         机器人当天做了什么（工具调用、回复、定时任务）
- ``reply_feedback`` 对某条回复的好评/差评
"""

import json
import logging
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    platform_message_id TEXT,
    attachments TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT DEFAULT '',
    confidence REAL DEFAULT 0.8,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_type, scope_id, status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT DEFAULT '',
    chat_id TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    ok INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS reply_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    rating TEXT NOT NULL,
    reviewer TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON reply_feedback(created_at);

CREATE TABLE IF NOT EXISTS reply_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    score INTEGER NOT NULL,
    note TEXT DEFAULT '',
    reviewer TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_created ON reply_scores(created_at);

CREATE TABLE IF NOT EXISTS review_days (
    day TEXT PRIMARY KEY,
    finished_at TEXT,
    exported_path TEXT DEFAULT '',
    scored_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS group_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    sender_name TEXT DEFAULT '',
    content TEXT NOT NULL,
    attachments TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_group_context ON group_context(group_id, id);

CREATE TABLE IF NOT EXISTS stickers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    ctype TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    source_group TEXT DEFAULT '',
    source_user TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    day TEXT NOT NULL,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(group_id, day)
);
"""

_MIGRATIONS = [
    ("messages", "platform_message_id", "TEXT"),
    ("messages", "attachments", "TEXT DEFAULT ''"),
    ("memories", "weight", "REAL DEFAULT 1.0"),
]


def _now() -> str:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).isoformat(timespec="seconds")


def _today() -> str:
    return _now()[:10]


def _load_attachments(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


class MemoryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or settings.MEMORY_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self):
        for table, column, ctype in _MIGRATIONS:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
                logger.info(f"数据库迁移: {table}.{column}")

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------ 对话

    def save_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        platform_message_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> int:
        payload = json.dumps(attachments, ensure_ascii=False) if attachments else ""
        cur = self._conn.execute(
            "INSERT INTO messages(chat_id, role, content, platform_message_id, attachments, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, role, content, platform_message_id, payload, _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def attach_platform_message_id(self, chat_id: str, platform_message_id: str) -> int | None:
        """把最近一条还没绑定平台 id 的 assistant 消息补上 message_id。"""
        row = self._conn.execute(
            "SELECT id FROM messages WHERE chat_id=? AND role='assistant' "
            "AND platform_message_id IS NULL ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE messages SET platform_message_id=? WHERE id=?",
            (str(platform_message_id), row["id"]),
        )
        self._conn.commit()
        return int(row["id"])

    def find_message_by_platform_id(self, platform_message_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE platform_message_id=? ORDER BY id DESC LIMIT 1",
            (str(platform_message_id),),
        ).fetchone()
        return dict(row) if row else None

    def last_assistant_message(self, chat_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE chat_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None

    def recent_messages(self, chat_id: str, limit: int = 16) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_messages(self, chat_id: str) -> int:
        cur = self._conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ 反馈

    def add_feedback(
        self,
        message_id: int,
        chat_id: str,
        rating: str,
        reviewer: str = "",
        note: str = "",
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO reply_feedback(message_id, chat_id, rating, reviewer, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, chat_id, rating, reviewer, note, _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ 行为日志

    def add_event(self, kind: str, name: str = "", chat_id: str = "",
                  detail: Any = None, ok: bool = True) -> int:
        payload = json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else ""
        cur = self._conn.execute(
            "INSERT INTO events(kind, name, chat_id, detail, ok, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (kind, name, chat_id, payload, 1 if ok else 0, _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ 打分与数据集

    def upsert_score(self, message_id: int, chat_id: str, score: int,
                     reviewer: str = "", note: str = "") -> None:
        score = int(score)
        if not 0 <= score <= 10:
            raise ValueError("score 必须在 0-10 之间")
        now = _now()
        self._conn.execute(
            "INSERT INTO reply_scores(message_id, chat_id, score, note, reviewer, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET score=excluded.score, note=excluded.note, "
            "reviewer=excluded.reviewer, updated_at=excluded.updated_at",
            (message_id, chat_id, score, note, reviewer, now, now),
        )
        self._conn.commit()

    def get_score(self, message_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM reply_scores WHERE message_id=?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def review_items(self, day: str) -> list[dict[str, Any]]:
        """某天的所有机器人回复，带上它前面那条用户消息和已有分数。"""
        rows = self._conn.execute(
            """
            SELECT m.id AS message_id, m.chat_id, m.content AS assistant,
                   m.created_at, m.attachments,
                   (SELECT u.content FROM messages u
                     WHERE u.chat_id = m.chat_id AND u.role = 'user' AND u.id < m.id
                     ORDER BY u.id DESC LIMIT 1) AS user_message,
                   (SELECT u.attachments FROM messages u
                     WHERE u.chat_id = m.chat_id AND u.role = 'user' AND u.id < m.id
                     ORDER BY u.id DESC LIMIT 1) AS user_attachments,
                   s.score, s.note
              FROM messages m
              LEFT JOIN reply_scores s ON s.message_id = m.id
             WHERE m.role = 'assistant' AND substr(m.created_at, 1, 10) = ?
             ORDER BY m.id
            """,
            (day,),
        ).fetchall()
        return [dict(r) for r in rows]

    def score_stats(self, day: str) -> dict[str, Any]:
        items = self.review_items(day)
        scored = [i for i in items if i["score"] is not None]
        histogram = {str(i): 0 for i in range(11)}
        for item in scored:
            histogram[str(item["score"])] += 1
        avg = round(sum(i["score"] for i in scored) / len(scored), 2) if scored else None
        return {
            "date": day,
            "total": len(items),
            "scored": len(scored),
            "remaining": len(items) - len(scored),
            "average": avg,
            "histogram": histogram,
        }

    def export_dataset(self, day: str) -> list[dict[str, Any]]:
        """只导出已打分的样本，供评测/微调使用。"""
        rows = []
        for item in self.review_items(day):
            if item["score"] is None:
                continue
            rows.append({
                "date": day,
                "chat_id": item["chat_id"],
                "user": item["user_message"] or "",
                "user_attachments": _load_attachments(item.get("user_attachments")),
                "assistant": item["assistant"],
                "score": item["score"],
                "note": item["note"] or "",
                "message_id": item["message_id"],
            })
        return rows

    def mark_review_finished(self, day: str, path: str, count: int) -> None:
        self._conn.execute(
            "INSERT INTO review_days(day, finished_at, exported_path, scored_count) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET "
            "finished_at=excluded.finished_at, exported_path=excluded.exported_path, "
            "scored_count=excluded.scored_count",
            (day, _now(), path, int(count)),
        )
        self._conn.commit()

    def review_status(self, day: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM review_days WHERE day=?", (day,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ 记忆

    def remember(
        self,
        scope_type: str,
        scope_id: str,
        text: str,
        *,
        source: str = "",
        confidence: float = 0.8,
    ) -> int:
        text = (text or "").strip()
        if not text:
            raise ValueError("记忆内容不能为空")

        existing = self._conn.execute(
            "SELECT id FROM memories WHERE scope_type=? AND scope_id=? "
            "AND text=? AND status='active'",
            (scope_type, scope_id, text),
        ).fetchone()
        if existing:
            return int(existing["id"])

        cur = self._conn.execute(
            "INSERT INTO memories(scope_type, scope_id, text, source, confidence, "
            "status, weight, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (scope_type, scope_id, text, source, confidence,
             1.5 if source == "tool" else 1.0, _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recall(
        self,
        scope_type: str,
        scope_id: str,
        query: str = "",
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """按「权重 × 时间衰减 × 关键词加成」排序，而不是单纯按时间。"""
        if limit <= 0:
            return []
        limit = min(limit, 50)

        rows = self._conn.execute(
            "SELECT id, text, source, created_at, use_count, weight FROM memories "
            "WHERE scope_type=? AND scope_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 500",
            (scope_type, scope_id),
        ).fetchall()
        if not rows:
            return []

        try:
            tz = ZoneInfo(settings.TIMEZONE)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)
        needle = query.strip().lower()

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                age_days = max(0.0, (now - created).total_seconds() / 86400)
            except (TypeError, ValueError):
                age_days = 0.0
            score = float(row["weight"] or 1.0) * math.exp(
                -age_days / max(1, settings.MEMORY_DECAY_DAYS)
            )
            if needle and needle in (row["text"] or "").lower():
                score *= 1.8
            scored.append((score, row))

        scored.sort(key=lambda item: (-item[0], -item[1]["id"]))
        result = [dict(row) for _, row in scored[:limit]]

        ids = [r["id"] for r in result]
        placeholders = ",".join("?" for _ in ids)
        self._conn.execute(
            f"UPDATE memories SET last_used_at=?, use_count=use_count+1 "
            f"WHERE id IN ({placeholders})",
            (_now(), *ids),
        )
        self._conn.commit()
        return result

    def forget(self, scope_type: str, scope_id: str, keyword: str = "") -> int:
        if keyword:
            pattern = "%" + keyword.replace("%", r"\%").replace("_", r"\_") + "%"
            cur = self._conn.execute(
                "DELETE FROM memories WHERE scope_type=? AND scope_id=? "
                "AND text LIKE ? ESCAPE '\\'",
                (scope_type, scope_id, pattern),
            )
        else:
            cur = self._conn.execute(
                "DELETE FROM memories WHERE scope_type=? AND scope_id=?",
                (scope_type, scope_id),
            )
        self._conn.commit()
        return cur.rowcount

    def count_memories(self, scope_type: str | None = None, scope_id: str | None = None) -> int:
        if scope_type and scope_id:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE scope_type=? AND scope_id=? AND status='active'",
                (scope_type, scope_id),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE status='active'"
            ).fetchone()
        return int(row["n"])

    def prune_memories(self, *, max_per_scope: int | None = None,
                       min_effective: float = 0.05, max_age_days: int = 180) -> int:
        """删除长期低权重记忆，并把每个 scope 截到上限。返回删除条数。"""
        max_per_scope = max_per_scope or settings.MEMORY_MAX_PER_SCOPE
        rows = self._conn.execute(
            "SELECT id, scope_type, scope_id, created_at, weight FROM memories WHERE status='active'"
        ).fetchall()

        try:
            tz = ZoneInfo(settings.TIMEZONE)
        except Exception:
            tz = ZoneInfo("UTC")
        now = datetime.now(tz)

        keep: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
        delete_ids: list[int] = []
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                age_days = max(0.0, (now - created).total_seconds() / 86400)
            except (TypeError, ValueError):
                age_days = 0.0
            score = float(row["weight"] or 1.0) * math.exp(
                -age_days / max(1, settings.MEMORY_DECAY_DAYS)
            )
            if score < min_effective and age_days > max_age_days:
                delete_ids.append(row["id"])
            else:
                keep[(row["scope_type"], row["scope_id"])].append((score, row["id"]))

        for items in keep.values():
            items.sort(reverse=True)
            delete_ids.extend(mid for _, mid in items[max_per_scope:])

        if delete_ids:
            placeholders = ",".join("?" for _ in delete_ids)
            self._conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", delete_ids)
            self._conn.commit()
        return len(delete_ids)

    # ------------------------------------------------------------------ 活动辅助

    def last_activity_at(self) -> str | None:
        row = self._conn.execute(
            "SELECT created_at FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["created_at"] if row else None

    # ------------------------------------------------------------------ 群聊短期上下文

    def add_group_context(self, group_id: str, user_id: str, content: str,
                          attachments: list[dict[str, Any]] | None = None,
                          sender_name: str = "") -> None:
        payload = json.dumps(attachments, ensure_ascii=False) if attachments else ""
        self._conn.execute(
            "INSERT INTO group_context(group_id, user_id, sender_name, content, attachments, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, user_id, sender_name, content, payload, _now()),
        )
        # 只保留每个群最近 N 条
        self._conn.execute(
            "DELETE FROM group_context WHERE group_id=? AND id NOT IN ("
            "SELECT id FROM group_context WHERE group_id=? ORDER BY id DESC LIMIT ?)",
            (group_id, group_id, max(1, settings.GROUP_CONTEXT_MAX)),
        )
        self._conn.commit()

    def recent_group_context(self, group_id: str, limit: int = 10,
                             minutes: int | None = None) -> list[dict[str, Any]]:
        minutes = minutes if minutes is not None else settings.GROUP_CONTEXT_MINUTES
        try:
            tz = ZoneInfo(settings.TIMEZONE)
        except Exception:
            tz = ZoneInfo("UTC")
        cutoff = (datetime.now(tz) - timedelta(minutes=max(1, minutes))).isoformat(timespec="seconds")
        rows = self._conn.execute(
            "SELECT user_id, sender_name, content, attachments, created_at "
            "FROM group_context WHERE group_id=? AND created_at>=? "
            "ORDER BY id DESC LIMIT ?",
            (group_id, cutoff, max(1, limit)),
        ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["attachments"] = _load_attachments(item.get("attachments"))
            result.append(item)
        return result

    def clear_group_context(self, group_id: str | None = None) -> int:
        if group_id:
            cur = self._conn.execute("DELETE FROM group_context WHERE group_id=?", (group_id,))
        else:
            cur = self._conn.execute("DELETE FROM group_context")
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ 表情库

    def save_sticker(self, file_hash: str, path: str, ctype: str = "",
                     summary: str = "", source_group: str = "",
                     source_user: str = "") -> tuple[int, bool]:
        """返回 (id, 是否新插入)。按内容 hash 去重。"""
        existing = self._conn.execute(
            "SELECT id FROM stickers WHERE hash=?", (file_hash,)
        ).fetchone()
        if existing:
            return int(existing["id"]), False
        cur = self._conn.execute(
            "INSERT INTO stickers(hash, path, ctype, summary, source_group, source_user, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_hash, path, ctype, summary, source_group, source_user, _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid), True

    def get_sticker(self, sticker_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM stickers WHERE id=?", (sticker_id,)
        ).fetchone()
        return dict(row) if row else None

    def count_stickers(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM stickers").fetchone()
        return int(row["n"])

    def list_stickers(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM stickers ORDER BY id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 日报

    def group_context_for_day(self, group_id: str, day: str,
                              limit: int = 2000) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT user_id, sender_name, content, attachments, created_at "
            "FROM group_context WHERE group_id=? AND substr(created_at,1,10)=? "
            "ORDER BY id ASC LIMIT ?",
            (group_id, day, max(1, limit)),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["attachments"] = _load_attachments(item.get("attachments"))
            result.append(item)
        return result

    def save_group_report(self, group_id: str, day: str, report: str) -> None:
        self._conn.execute(
            "INSERT INTO group_reports(group_id, day, report, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(group_id, day) DO UPDATE SET report=excluded.report, "
            "created_at=excluded.created_at",
            (group_id, day, report, _now()),
        )
        self._conn.commit()

    def get_group_report(self, group_id: str, day: str) -> str | None:
        row = self._conn.execute(
            "SELECT report FROM group_reports WHERE group_id=? AND day=?",
            (group_id, day),
        ).fetchone()
        return row["report"] if row else None

    def has_event_today(self, kind: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE kind=? AND substr(created_at,1,10)=? LIMIT 1",
            (kind, _today()),
        ).fetchone()
        return row is not None

    def count_events_today(self, kind: str, chat_id: str | None = None) -> int:
        if chat_id is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE kind=? AND substr(created_at,1,10)=?",
                (kind, _today()),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE kind=? AND chat_id=? "
                "AND substr(created_at,1,10)=?",
                (kind, chat_id, _today()),
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ 日报

    def daily_summary(self, day: str | None = None) -> dict[str, Any]:
        day = day or _today()
        msg_rows = self._conn.execute(
            "SELECT role, COUNT(*) AS n FROM messages WHERE substr(created_at,1,10)=? "
            "GROUP BY role", (day,),
        ).fetchall()
        counts = {r["role"]: int(r["n"]) for r in msg_rows}
        chats = self._conn.execute(
            "SELECT COUNT(DISTINCT chat_id) AS n FROM messages WHERE substr(created_at,1,10)=?",
            (day,),
        ).fetchone()["n"]

        event_rows = self._conn.execute(
            "SELECT kind, COUNT(*) AS n FROM events WHERE substr(created_at,1,10)=? "
            "GROUP BY kind", (day,),
        ).fetchall()
        events = {r["kind"]: int(r["n"]) for r in event_rows}

        tool_rows = self._conn.execute(
            "SELECT name, COUNT(*) AS n FROM events WHERE kind='tool' AND substr(created_at,1,10)=? "
            "AND name<>'' GROUP BY name ORDER BY n DESC", (day,),
        ).fetchall()
        tools = {r["name"]: int(r["n"]) for r in tool_rows}

        fb_rows = self._conn.execute(
            "SELECT rating, COUNT(*) AS n FROM reply_feedback WHERE substr(created_at,1,10)=? "
            "GROUP BY rating", (day,),
        ).fetchall()
        feedback = {r["rating"]: int(r["n"]) for r in fb_rows}

        return {
            "date": day,
            "messages": {
                "received": counts.get("user", 0),
                "sent": counts.get("assistant", 0),
                "chats": int(chats),
            },
            "events": events,
            "tools": tools,
            "feedback": {"positive": feedback.get("positive", 0),
                         "negative": feedback.get("negative", 0)},
        }
