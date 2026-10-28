"""短期对话、长期记忆、行为日志与回复评价，统一落在 SQLite。

表：

- ``messages``       与机器人交互的消息（收/发），含平台 message_id
- ``memories``       按 scope（user/group）隔离的事实
- ``events``         机器人当天做了什么（工具调用、回复、定时任务）
- ``reply_feedback`` 对某条回复的好评/差评
"""

import hashlib
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
from app.memory.vectors import cosine, similarity_score, unpack_vector

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

CREATE TABLE IF NOT EXISTS sticker_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    ctype TEXT DEFAULT '',
    group_id TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    message_id TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_groups (
    group_id TEXT PRIMARY KEY,
    mode TEXT DEFAULT 'normal',
    participation INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unknown_groups (
    group_id TEXT PRIMARY KEY,
    user_id TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    sample TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    notified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tone_rules (
    tone TEXT PRIMARY KEY,
    keywords TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL,
    member_openid TEXT NOT NULL,
    username TEXT DEFAULT '',
    last_seen TEXT NOT NULL,
    message_count INTEGER DEFAULT 1,
    PRIMARY KEY(group_id, member_openid)
);

CREATE TABLE IF NOT EXISTS group_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    day TEXT NOT NULL,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(group_id, day)
);

CREATE TABLE IF NOT EXISTS memory_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    name TEXT NOT NULL,
    keywords TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    summary_at TEXT,
    weight REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope_type, scope_id, name)
);

CREATE TABLE IF NOT EXISTS memory_vectors (
    owner_type TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY(owner_type, owner_id, model)
);

CREATE TABLE IF NOT EXISTS memory_extract_cursors (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    last_message_id INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS chat_summaries (
    chat_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    up_to_message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATIONS = [    ("messages", "platform_message_id", "TEXT"),
    ("messages", "attachments", "TEXT DEFAULT ''"),
    ("memories", "weight", "REAL DEFAULT 1.0"),
    ("memories", "memory_hash", "TEXT"),
    ("memories", "importance", "REAL DEFAULT 0.6"),
    ("memories", "source_message_ids", "TEXT DEFAULT '[]'"),
    ("memories", "topic_id", "INTEGER"),
    ("stickers", "name", "TEXT DEFAULT ''"),
    ("stickers", "tags", "TEXT DEFAULT ''"),
    ("stickers", "use_count", "INTEGER DEFAULT 0"),
    ("stickers", "last_used_at", "TEXT"),
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


def _merge_keywords(old: str, new: str) -> str:
    items: list[str] = []
    for chunk in (old or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if chunk and chunk not in items:
            items.append(chunk)
    for chunk in (new or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if chunk and chunk not in items:
            items.append(chunk)
    return ",".join(items[:20])


DEFAULT_TONE_RULES = (
    ("无语", "无语,服了,离谱,真会,懒得,算了,汗,拿你没办法,沉默,……"),
    ("开心", "哈哈,笑死,开心,好耶,嘿嘿,乐"),
    ("傲娇", "哼,才不,略,笨蛋,不要"),
    ("得意", "牛,厉害,不错,可以啊,当然,包的"),
    ("疑惑", "？,?,什么,真的假的,啊"),
    ("丧", "唉,难过,惨,寄,累"),
)


class MemoryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or settings.MEMORY_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._seed_tone_rules()
        self._conn.commit()

    def _seed_tone_rules(self):
        row = self._conn.execute("SELECT COUNT(*) AS n FROM tone_rules").fetchone()
        if int(row["n"]) > 0:
            return
        now = _now()
        for tone, keywords in DEFAULT_TONE_RULES:
            self._conn.execute(
                "INSERT INTO tone_rules(tone, keywords, enabled, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (tone, keywords, now, now),
            )

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

    def recent_messages_after(self, chat_id: str, after_id: int,
                              limit: int = 16) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? AND id > ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, int(after_id), limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def messages_after_id(self, chat_id: str, after_id: int,
                          limit: int = 40) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE chat_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (chat_id, int(after_id), max(1, limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_messages_after(self, chat_id: str, after_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ? AND id > ?",
            (chat_id, int(after_id)),
        ).fetchone()
        return int(row["n"])

    def get_chat_summary(self, chat_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM chat_summaries WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_chat_summary(self, chat_id: str, summary: str,
                            up_to_message_id: int) -> None:
        self._conn.execute(
            "INSERT INTO chat_summaries(chat_id, summary, up_to_message_id, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "summary=excluded.summary, up_to_message_id=excluded.up_to_message_id, "
            "updated_at=excluded.updated_at",
            (chat_id, summary.strip(), int(up_to_message_id), _now()),
        )
        self._conn.commit()

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
        importance: float | None = None,
        source_message_ids: list[int] | None = None,
        topic_id: int | None = None,
    ) -> int:
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("记忆内容不能为空")

        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        existing = self._conn.execute(
            "SELECT id, importance FROM memories WHERE scope_type=? AND scope_id=? "
            "AND (text=? OR memory_hash=?) AND status='active'",
            (scope_type, scope_id, text, digest),
        ).fetchone()
        if existing:
            if importance is not None and importance > float(existing["importance"] or 0):
                self._conn.execute(
                    "UPDATE memories SET importance=? WHERE id=?",
                    (float(importance), existing["id"]),
                )
            if topic_id is not None:
                self._conn.execute(
                    "UPDATE memories SET topic_id=? WHERE id=?", (topic_id, existing["id"])
                )
            self._conn.commit()
            return int(existing["id"])

        sources = json.dumps(source_message_ids or [], ensure_ascii=False)
        cur = self._conn.execute(
            "INSERT INTO memories(scope_type, scope_id, text, source, confidence, "
            "status, weight, importance, memory_hash, source_message_ids, topic_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
            (
                scope_type, scope_id, text, source, confidence,
                1.5 if source == "tool" else 1.0,
                float(importance if importance is not None else 0.6),
                digest, sources, topic_id, _now(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_memories(self, scope_type: str, scope_id: str,
                      limit: int = 500) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, text, source, confidence, importance, created_at, "
            "use_count, weight, topic_id, source_message_ids FROM memories "
            "WHERE scope_type=? AND scope_id=? AND status='active' "
            "ORDER BY id DESC LIMIT ?",
            (scope_type, scope_id, max(1, limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def messages_since(self, chat_id: str, last_id: int,
                       limit: int = 40) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE chat_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (chat_id, int(last_id), max(1, limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def pending_message_count(self, chat_id: str, last_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id=? AND id>?",
            (chat_id, int(last_id)),
        ).fetchone()
        return int(row["n"])

    def recent_chat_ids(self, limit: int = 30) -> list[str]:
        rows = self._conn.execute(
            "SELECT chat_id, MAX(id) AS last_id FROM messages "
            "GROUP BY chat_id ORDER BY last_id DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
        return [row["chat_id"] for row in rows]

    def get_extract_cursor(self, scope_type: str, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT last_message_id FROM memory_extract_cursors "
            "WHERE scope_type=? AND scope_id=?",
            (scope_type, scope_id),
        ).fetchone()
        return int(row["last_message_id"]) if row else 0

    def set_extract_cursor(self, scope_type: str, scope_id: str, last_message_id: int) -> None:
        self._conn.execute(
            "INSERT INTO memory_extract_cursors(scope_type, scope_id, last_message_id, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
            "last_message_id=excluded.last_message_id, updated_at=excluded.updated_at",
            (scope_type, scope_id, int(last_message_id), _now()),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ 主题

    def upsert_topic(self, scope_type: str, scope_id: str, name: str,
                     keywords: str = "", summary: str = "") -> int:
        name = " ".join((name or "").split())[:20]
        if not name:
            raise ValueError("主题名不能为空")
        row = self._conn.execute(
            "SELECT id, keywords FROM memory_topics WHERE scope_type=? AND scope_id=? AND name=?",
            (scope_type, scope_id, name),
        ).fetchone()
        if row:
            merged = _merge_keywords(row["keywords"] or "", keywords)
            self._conn.execute(
                "UPDATE memory_topics SET keywords=?, updated_at=? WHERE id=?",
                (merged, _now(), row["id"]),
            )
            self._conn.commit()
            return int(row["id"])

        cur = self._conn.execute(
            "INSERT INTO memory_topics(scope_type, scope_id, name, keywords, summary, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (scope_type, scope_id, name, keywords.strip(), summary.strip(), _now(), _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_topic(self, topic_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM memory_topics WHERE id=?", (topic_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_topic_by_name(self, scope_type: str, scope_id: str,
                          name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM memory_topics WHERE scope_type=? AND scope_id=? AND name=?",
            (scope_type, scope_id, name),
        ).fetchone()
        return dict(row) if row else None

    def list_topics(self, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM memory_topics WHERE scope_type=? AND scope_id=? "
            "ORDER BY weight DESC, id ASC",
            (scope_type, scope_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_topic_scopes(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT DISTINCT scope_type, scope_id FROM memory_topics"
        ).fetchall()
        return [(row["scope_type"], row["scope_id"]) for row in rows]

    def list_memory_scopes(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT DISTINCT scope_type, scope_id FROM memories WHERE status='active'"
        ).fetchall()
        return [(row["scope_type"], row["scope_id"]) for row in rows]

    def count_topics(self, scope_type: str, scope_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM memory_topics WHERE scope_type=? AND scope_id=?",
            (scope_type, scope_id),
        ).fetchone()
        return int(row["n"])

    def count_all_topics(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM memory_topics").fetchone()
        return int(row["n"])

    def count_topic_memories(self, topic_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE topic_id=? AND status='active'",
            (topic_id,),
        ).fetchone()
        return int(row["n"])

    def update_topic_summary(self, topic_id: int, summary: str) -> None:
        self._conn.execute(
            "UPDATE memory_topics SET summary=?, summary_at=?, updated_at=? WHERE id=?",
            (summary.strip(), _now(), _now(), topic_id),
        )
        self._conn.commit()

    def attach_memory_topic(self, memory_id: int, topic_id: int | None) -> None:
        self._conn.execute(
            "UPDATE memories SET topic_id=? WHERE id=?", (topic_id, memory_id)
        )
        self._conn.commit()

    def bump_memory_importance(self, memory_id: int, importance: float) -> None:
        self._conn.execute(
            "UPDATE memories SET importance=MAX(importance, ?) WHERE id=?",
            (float(importance), memory_id),
        )
        self._conn.commit()

    def topic_memories(self, topic_id: int, limit: int = 40) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, text, importance FROM memories WHERE topic_id=? AND status='active' "
            "ORDER BY importance DESC, id DESC LIMIT ?",
            (topic_id, max(1, limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_empty_topics(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM memory_topics WHERE id NOT IN "
            "(SELECT DISTINCT topic_id FROM memories WHERE topic_id IS NOT NULL AND status='active')"
        )
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ 向量

    def set_vector(self, owner_type: str, owner_id: int, model: str,
                   vector: bytes, dim: int) -> None:
        self._conn.execute(
            "INSERT INTO memory_vectors(owner_type, owner_id, model, dim, vector) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(owner_type, owner_id, model) "
            "DO UPDATE SET dim=excluded.dim, vector=excluded.vector",
            (owner_type, int(owner_id), model, int(dim), vector),
        )
        self._conn.commit()

    def get_vectors(self, owner_type: str, model: str,
                    ids: list[int]) -> dict[int, bytes]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT owner_id, vector FROM memory_vectors WHERE owner_type=? "
            f"AND model=? AND owner_id IN ({placeholders})",
            (owner_type, model, *ids),
        ).fetchall()
        return {int(row["owner_id"]): row["vector"] for row in rows}

    def memory_ids_missing_vectors(self, model: str, limit: int = 100) -> list[int]:
        rows = self._conn.execute(
            "SELECT m.id FROM memories m WHERE m.status='active' AND NOT EXISTS "
            "(SELECT 1 FROM memory_vectors v WHERE v.owner_type='memory' "
            "AND v.owner_id=m.id AND v.model=?) ORDER BY m.id DESC LIMIT ?",
            (model, max(1, limit)),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def topic_ids_missing_vectors(self, model: str, limit: int = 100) -> list[int]:
        rows = self._conn.execute(
            "SELECT t.id FROM memory_topics t WHERE NOT EXISTS "
            "(SELECT 1 FROM memory_vectors v WHERE v.owner_type='topic' "
            "AND v.owner_id=t.id AND v.model=?) ORDER BY t.id DESC LIMIT ?",
            (model, max(1, limit)),
        ).fetchall()
        return [int(row["id"]) for row in rows]

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

    def recall_hierarchical(
        self,
        scope_type: str,
        scope_id: str,
        query: str = "",
        *,
        limit: int = 5,
        topics_limit: int = 3,
        query_vector: list[float] | None = None,
        model: str | None = None,
        use_topics: bool = True,
    ) -> list[dict[str, Any]]:
        """扁平向量召回 + 主题软加权；主题只加分，不做门禁。

        query_vector/model 为空时退化为关键词 + 时间衰减（与旧 recall 接近）。
        """
        if limit <= 0:
            return []
        limit = min(limit, 50)

        rows = self._conn.execute(
            "SELECT id, text, source, created_at, use_count, weight, importance, topic_id "
            "FROM memories WHERE scope_type=? AND scope_id=? AND status='active' "
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
        needle = (query or "").strip().lower()

        topics = {}
        if use_topics and topics_limit > 0:
            topics = {t["id"]: t for t in self.list_topics(scope_type, scope_id)}

        memory_ids = [int(r["id"]) for r in rows]
        mem_vectors = (
            self.get_vectors("memory", model, memory_ids)
            if query_vector and model else {}
        )
        topic_vectors = (
            self.get_vectors("topic", model, list(topics))
            if query_vector and model and topics else {}
        )

        topic_scores: dict[int, float] = {}
        for topic_id, topic in topics.items():
            score = 0.0
            haystack = " ".join(
                [topic.get("name") or "", topic.get("keywords") or "",
                 topic.get("summary") or ""]
            ).lower()
            if needle and needle in haystack:
                score = max(score, 1.0)
            blob = topic_vectors.get(topic_id)
            if blob is not None:
                score = max(
                    score,
                    similarity_score(cosine(query_vector, unpack_vector(blob))),
                )
            if score > 0:
                topic_scores[topic_id] = score

        top_topics = {
            tid for tid, _ in sorted(
                topic_scores.items(), key=lambda kv: (-kv[1], kv[0])
            )[:topics_limit]
        }

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            try:
                created = datetime.fromisoformat(item["created_at"])
                age_days = max(0.0, (now - created).total_seconds() / 86400)
            except (TypeError, ValueError):
                age_days = 0.0
            decay = math.exp(-age_days / max(1, settings.MEMORY_DECAY_DAYS))
            importance = max(0.0, min(1.0, float(item.get("importance") or 0.6)))
            text = (item.get("text") or "").lower()
            keyword_hit = bool(needle and needle in text)

            blob = mem_vectors.get(int(item["id"]))
            if blob is not None:
                sim = similarity_score(cosine(query_vector, unpack_vector(blob)))
                score = 0.65 * sim + 0.25 * decay + 0.10 * importance
                if keyword_hit:
                    score += 0.05
            else:
                score = float(item.get("weight") or 1.0) * decay
                score *= 1.8 if keyword_hit else 1.0
                score *= 0.5 + 0.5 * importance

            topic = topics.get(item.get("topic_id"))
            if topic is not None and topic["id"] in top_topics:
                score += 0.10
                haystack = " ".join(
                    [topic.get("name") or "", topic.get("keywords") or ""]
                ).lower()
                if needle and needle in haystack:
                    score += 0.05
                item["topic_name"] = topic.get("name") or ""
                item["topic_summary"] = topic.get("summary") or ""
            else:
                item["topic_name"] = topic.get("name") if topic else ""
                item["topic_summary"] = topic.get("summary") if topic else ""

            item["score"] = round(score, 6)
            scored.append((score, item))

        scored.sort(key=lambda pair: (-pair[0], -pair[1]["id"]))
        result = [item for _, item in scored[:limit]]

        ids = [r["id"] for r in result]
        if ids:
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
                          sender_name: str = "",
                          created_at: str | None = None) -> None:
        payload = json.dumps(attachments, ensure_ascii=False) if attachments else ""
        self._conn.execute(
            "INSERT INTO group_context(group_id, user_id, sender_name, content, attachments, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, user_id, sender_name, content, payload, created_at or _now()),
        )
        # 普通群按条数滚动；只读/分析群按天保留，供日报使用
        long_keep = (
            group_id in settings.QQ_READONLY_GROUPS
            or group_id in settings.GROUP_ANALYSIS_GROUPS
        )
        if long_keep:
            try:
                tz = ZoneInfo(settings.TIMEZONE)
            except Exception:
                tz = ZoneInfo("UTC")
            cutoff = (
                datetime.now(tz) - timedelta(days=max(1, settings.GROUP_CONTEXT_KEEP_DAYS))
            ).isoformat(timespec="seconds")
            self._conn.execute(
                "DELETE FROM group_context WHERE group_id=? AND created_at<?",
                (group_id, cutoff),
            )
        else:
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

    def recent_message_attachments(self, chat_id: str, limit: int = 10,
                                   minutes: int = 10) -> list[dict[str, Any]]:
        """取某会话最近几条带附件的用户消息，供私聊“存刚发的图”用。"""
        try:
            tz = ZoneInfo(settings.TIMEZONE)
        except Exception:
            tz = ZoneInfo("UTC")
        cutoff = (datetime.now(tz) - timedelta(minutes=max(1, minutes))).isoformat(timespec="seconds")
        rows = self._conn.execute(
            "SELECT attachments FROM messages WHERE chat_id=? AND role='user' "
            "AND attachments IS NOT NULL AND attachments != '' AND created_at>=? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, cutoff, max(1, limit)),
        ).fetchall()
        result = []
        for row in rows:
            result.extend(_load_attachments(row["attachments"]))
        return result

    def group_context_from_user(self, group_id: str, user_id: str,
                                limit: int = 3) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT user_id, sender_name, content, attachments, created_at "
            "FROM group_context WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (group_id, user_id, max(1, limit)),
        ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["attachments"] = _load_attachments(item.get("attachments"))
            result.append(item)
        return result

    def group_context_between(self, group_id: str, start: str, end: str,
                              limit: int = 60) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT user_id, sender_name, content, attachments, created_at "
            "FROM group_context WHERE group_id=? AND created_at>=? AND created_at<=? "
            "ORDER BY id DESC LIMIT ?",
            (group_id, start, end, max(1, limit)),
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
                     source_user: str = "", name: str = "",
                     tags: str = "") -> tuple[int, bool, bool]:
        """返回 (id, 是否新插入, 是否改名)。按内容 hash 去重。"""
        existing = self._conn.execute(
            "SELECT id, name, tags FROM stickers WHERE hash=?", (file_hash,)
        ).fetchone()
        if existing:
            renamed = False
            if name and name != (existing["name"] or ""):
                self._conn.execute(
                    "UPDATE stickers SET name=? WHERE id=?", (name, existing["id"])
                )
                renamed = True
            if tags:
                self._conn.execute(
                    "UPDATE stickers SET tags=? WHERE id=?", (tags, existing["id"])
                )
            self._conn.commit()
            return int(existing["id"]), False, renamed
        cur = self._conn.execute(
            "INSERT INTO stickers(hash, path, ctype, summary, source_group, source_user, "
            "name, tags, use_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (file_hash, path, ctype, summary, source_group, source_user,
             name, tags, _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid), True, bool(name)

    def set_sticker_tags(self, sticker_id: int, tags: str) -> bool:
        cur = self._conn.execute(
            "UPDATE stickers SET tags=? WHERE id=?", (tags, sticker_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def mark_sticker_used(self, sticker_id: int) -> None:
        self._conn.execute(
            "UPDATE stickers SET use_count=COALESCE(use_count, 0)+1, last_used_at=? "
            "WHERE id=?",
            (_now(), sticker_id),
        )
        self._conn.commit()

    def get_sticker_by_name(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM stickers WHERE name=? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        return dict(row) if row else None

    def get_sticker_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM stickers WHERE hash=?", (file_hash,)
        ).fetchone()
        return dict(row) if row else None

    def set_sticker_path_name(self, sticker_id: int, path: str, name: str | None = None) -> None:
        if name is None:
            self._conn.execute("UPDATE stickers SET path=? WHERE id=?", (path, sticker_id))
        else:
            self._conn.execute(
                "UPDATE stickers SET path=?, name=? WHERE id=?", (path, name, sticker_id)
            )
        self._conn.commit()

    def update_sticker(self, file_hash: str, path: str, name: str = "") -> int | None:
        """文件被改名/移动后，按 hash 修正路径和名字。"""
        row = self._conn.execute(
            "SELECT id, name FROM stickers WHERE hash=?", (file_hash,)
        ).fetchone()
        if row is None:
            return None
        new_name = row["name"] or name or ""
        self._conn.execute(
            "UPDATE stickers SET path=?, name=? WHERE id=?",
            (path, new_name, row["id"]),
        )
        self._conn.commit()
        return int(row["id"])

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

    def add_sticker_candidate(self, file_hash: str, path: str, ctype: str = "",
                              group_id: str = "", user_id: str = "",
                              sender_name: str = "", message_id: str = ""
                              ) -> tuple[int, bool]:
        existing = self._conn.execute(
            "SELECT id FROM sticker_candidates WHERE hash=?", (file_hash,)
        ).fetchone()
        if existing:
            return int(existing["id"]), False
        cur = self._conn.execute(
            "INSERT INTO sticker_candidates(hash, path, ctype, group_id, user_id, "
            "sender_name, message_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (file_hash, path, ctype, group_id, user_id, sender_name, message_id, _now()),
        )
        self._conn.commit()
        return int(cur.lastrowid), True

    def get_sticker_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sticker_candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_sticker_candidates(self, status: str = "pending",
                                limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sticker_candidates WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, max(1, limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_sticker_candidates(self, status: str = "pending") -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM sticker_candidates WHERE status=?", (status,)
        ).fetchone()
        return int(row["n"])

    def set_sticker_candidate_status(self, candidate_id: int, status: str,
                                     path: str | None = None) -> None:
        if path is None:
            self._conn.execute(
                "UPDATE sticker_candidates SET status=? WHERE id=?",
                (status, candidate_id),
            )
        else:
            self._conn.execute(
                "UPDATE sticker_candidates SET status=?, path=? WHERE id=?",
                (status, path, candidate_id),
            )
        self._conn.commit()

    # ------------------------------------------------------------------ 运行时群配置

    def upsert_runtime_group(self, group_id: str, mode: str = "normal",
                             participation: bool = False) -> None:
        self._conn.execute(
            "INSERT INTO runtime_groups(group_id, mode, participation, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET "
            "mode=excluded.mode, participation=excluded.participation, "
            "updated_at=excluded.updated_at",
            (group_id, mode, 1 if participation else 0, _now(), _now()),
        )
        self._conn.commit()

    def get_runtime_group(self, group_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM runtime_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_runtime_groups(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runtime_groups ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_runtime_group(self, group_id: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM runtime_groups WHERE group_id=?", (group_id,)
        )
        self._conn.commit()
        return cur.rowcount

    def record_unknown_group(self, group_id: str, user_id: str = "",
                             sender_name: str = "", sample: str = "") -> bool:
        """首次记录返回 True；已存在只更新最后出现时间，返回 False。"""
        existing = self._conn.execute(
            "SELECT group_id FROM unknown_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE unknown_groups SET last_seen=?, user_id=?, sender_name=?, sample=? "
                "WHERE group_id=?",
                (_now(), user_id, sender_name, sample[:200], group_id),
            )
            self._conn.commit()
            return False
        self._conn.execute(
            "INSERT INTO unknown_groups(group_id, user_id, sender_name, sample, status, "
            "first_seen, last_seen, notified) VALUES (?, ?, ?, ?, 'pending', ?, ?, 0)",
            (group_id, user_id, sender_name, sample[:200], _now(), _now()),
        )
        self._conn.commit()
        return True

    def get_unknown_group(self, group_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM unknown_groups WHERE group_id=?", (group_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_unknown_group_status(self, group_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE unknown_groups SET status=?, notified=1 WHERE group_id=?",
            (status, group_id),
        )
        self._conn.commit()

    def mark_unknown_group_notified(self, group_id: str) -> None:
        self._conn.execute(
            "UPDATE unknown_groups SET notified=1 WHERE group_id=?", (group_id,)
        )
        self._conn.commit()

    def list_unknown_groups(self, status: str = "pending") -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM unknown_groups WHERE status=? ORDER BY first_seen DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 语气规则

    def list_tone_rules(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tone_rules"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY tone"
        rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def upsert_tone_rule(self, tone: str, keywords: str, enabled: bool = True) -> None:
        now = _now()
        self._conn.execute(
            "INSERT INTO tone_rules(tone, keywords, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(tone) DO UPDATE SET "
            "keywords=excluded.keywords, enabled=excluded.enabled, updated_at=excluded.updated_at",
            (tone.strip(), keywords.strip(), 1 if enabled else 0, now, now),
        )
        self._conn.commit()

    def delete_tone_rule(self, tone: str) -> int:
        cur = self._conn.execute("DELETE FROM tone_rules WHERE tone=?", (tone,))
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------ 群成员目录

    def upsert_group_member(self, group_id: str, member_openid: str,
                            username: str = "") -> None:
        now = _now()
        self._conn.execute(
            "INSERT INTO group_members(group_id, member_openid, username, last_seen, message_count) "
            "VALUES (?, ?, ?, ?, 1) ON CONFLICT(group_id, member_openid) DO UPDATE SET "
            "username=CASE WHEN excluded.username != '' THEN excluded.username ELSE username END, "
            "last_seen=excluded.last_seen, message_count=message_count+1",
            (group_id, member_openid, username or "", now),
        )
        self._conn.commit()

    def search_group_members(self, group_id: str, query: str,
                             limit: int = 10) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        exact = self._conn.execute(
            "SELECT * FROM group_members WHERE group_id=? AND username=? "
            "ORDER BY last_seen DESC LIMIT ?",
            (group_id, query, max(1, limit)),
        ).fetchall()
        if exact:
            return [dict(r) for r in exact]
        rows = self._conn.execute(
            "SELECT * FROM group_members WHERE group_id=? AND username LIKE ? "
            "ORDER BY last_seen DESC LIMIT ?",
            (group_id, f"%{query}%", max(1, limit)),
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

    def last_event_at(self, kind: str, chat_id: str | None = None) -> str | None:
        if chat_id is None:
            row = self._conn.execute(
                "SELECT created_at FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT created_at FROM events WHERE kind=? AND chat_id=? "
                "ORDER BY id DESC LIMIT 1",
                (kind, chat_id),
            ).fetchone()
        return str(row["created_at"]) if row else None

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
