"""短期对话历史 + 长期记忆，落在 SQLite。

表结构刻意保持简单：

- ``messages``：按 chat_id 存对话，用于组装最近 N 轮
- ``memories``：按 scope 存事实，scope 为 ``user:<qq>`` / ``group:<gid>`` / ``global``

检索先用 LIKE + 时间倒序（个人规模足够）；等记忆过千条再考虑 FTS5/向量。
"""

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
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

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
"""


def _now() -> str:
    try:
        tz = ZoneInfo(settings.TIMEZONE)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or settings.MEMORY_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        self._conn.close()

    # ------------------------------------------------------------------ 对话

    def save_message(self, chat_id: str, role: str, content: str) -> None:
        self._conn.execute(
            "INSERT INTO messages(chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, _now()),
        )
        self._conn.commit()

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
            # 关键词没命中时退回最近记忆，保证至少有点上下文
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
