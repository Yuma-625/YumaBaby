"""短期对话、长期记忆、行为日志与回复评价，统一落在 SQLite。

表：

- ``messages``       与机器人交互的消息（收/发），含平台 message_id
- ``memories``       按 scope（user/group）隔离的事实
- ``events``         机器人当天做了什么（工具调用、回复、定时任务）
- ``reply_feedback`` 对某条回复的好评/差评
"""

import json
import logging
import sqlite3
from datetime import datetime
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
"""

_MIGRATIONS = [
    ("messages", "platform_message_id", "TEXT"),
]


def _now() -> str:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).isoformat(timespec="seconds")


def _today() -> str:
    return _now()[:10]


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
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO messages(chat_id, role, content, platform_message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, content, platform_message_id, _now()),
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
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (scope_type, scope_id, text, source, confidence, _now()),
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
        if limit <= 0:
            return []
        limit = min(limit, 50)

        if query.strip():
            pattern = "%" + query.strip().replace("%", r"\%").replace("_", r"\_") + "%"
            rows = self._conn.execute(
                "SELECT id, text, source, created_at, use_count FROM memories "
                "WHERE scope_type=? AND scope_id=? AND status='active' AND text LIKE ? ESCAPE '\\' "
                "ORDER BY created_at DESC LIMIT ?",
                (scope_type, scope_id, pattern, limit),
            ).fetchall()
            if not rows:
                rows = self._conn.execute(
                    "SELECT id, text, source, created_at, use_count FROM memories "
                    "WHERE scope_type=? AND scope_id=? AND status='active' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (scope_type, scope_id, limit),
                ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, text, source, created_at, use_count FROM memories "
                "WHERE scope_type=? AND scope_id=? AND status='active' "
                "ORDER BY created_at DESC LIMIT ?",
                (scope_type, scope_id, limit),
            ).fetchall()

        result = [dict(r) for r in rows]
        if result:
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
