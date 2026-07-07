"""SQLite persistence for web adapter conversations.

Schema designed to extend into Phase 14 memory system (same db, add `facts` and
`episodes` tables later). For now stores:
  - conversations: one row per chat thread
  - messages: every user/assistant message, with serialized tool_calls + attachments

Each conversation re-creates an Orchestrator from its stored messages when loaded.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_DB = Path.home() / ".cache" / "agent-web" / "chats.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT 'web',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id         TEXT NOT NULL,
    role            TEXT NOT NULL,           -- 'user' | 'assistant' | 'tool'
    content         TEXT NOT NULL,           -- text or json string for tool results
    extras_json     TEXT,                    -- attachments, tool_progress, files, tool_calls
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (conv_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);
CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
"""


def _now() -> int:
    return int(time.time())


class ChatStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # ─── conversation ─────────────────────────────────────────
    def create_conversation(self, conv_id: str, title: str = "新對話",
                            channel: str = "web") -> dict:
        now = _now()
        self.db.execute(
            "INSERT INTO conversations(id, title, channel, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conv_id, title, channel, now, now),
        )
        self.db.commit()
        return {"id": conv_id, "title": title, "channel": channel,
                "created_at": now, "updated_at": now}

    def list_conversations(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, title, channel, created_at, updated_at "
            "FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conv_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT id, title, channel, created_at, updated_at "
            "FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
        return dict(row) if row else None

    def rename_conversation(self, conv_id: str, title: str):
        self.db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), conv_id),
        )
        self.db.commit()

    def delete_conversation(self, conv_id: str):
        self.db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        self.db.commit()

    def touch(self, conv_id: str):
        self.db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (_now(), conv_id),
        )
        self.db.commit()

    # ─── messages ─────────────────────────────────────────────
    def add_message(self, conv_id: str, role: str, content: str,
                    extras: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO messages(conv_id, role, content, extras_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conv_id, role, content,
             json.dumps(extras, ensure_ascii=False) if extras else None,
             _now()),
        )
        self.db.commit()
        self.touch(conv_id)
        return cur.lastrowid

    def get_messages(self, conv_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, role, content, extras_json, created_at FROM messages "
            "WHERE conv_id = ? ORDER BY id ASC",
            (conv_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["extras"] = json.loads(d.pop("extras_json")) if d["extras_json"] else {}
            out.append(d)
        return out

    def get_orchestrator_messages(self, conv_id: str) -> list[dict]:
        """Return messages in the shape Orchestrator.messages expects."""
        rows = self.get_messages(conv_id)
        out = []
        for r in rows:
            role = r["role"]
            if role == "user":
                out.append({"role": "user", "content": r["content"]})
            elif role == "assistant":
                # tool_calls are stored as serialized list of {name, args}
                msg: dict[str, Any] = {"role": "assistant", "content": r["content"]}
                tcs = (r["extras"] or {}).get("tool_calls") or []
                if tcs:
                    msg["tool_calls"] = tcs
                out.append(msg)
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_name": (r["extras"] or {}).get("tool_name", ""),
                    "content": r["content"],
                })
        return out

    def message_count(self, conv_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conv_id = ?",
            (conv_id,),
        ).fetchone()
        return row["n"] if row else 0


def auto_title_from_first_message(text: str, max_len: int = 30) -> str:
    """Generate a conversation title from the first user message."""
    t = (text or "").strip().split("\n")[0]
    if len(t) > max_len:
        t = t[:max_len] + "..."
    return t or "新對話"
