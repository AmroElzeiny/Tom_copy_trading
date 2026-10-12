from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, db_path: Path, audit_path: Path) -> None:
        self.db_path = db_path
        self.audit_path = audit_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    symbol_key TEXT,
                    created_utc TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_context (
                    chat_id INTEGER PRIMARY KEY,
                    symbol_key TEXT NOT NULL,
                    updated_utc TEXT NOT NULL
                )
                """
            )

    def claim(self, chat_id: int, message_id: int) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO messages(chat_id,message_id,status,created_utc) VALUES(?,?,?,?)",
                    (chat_id, message_id, "PROCESSING", now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def set_status(self, chat_id: int, message_id: int, status: str, symbol_key: str | None = None) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE messages SET status=?, symbol_key=? WHERE chat_id=? AND message_id=?",
                (status, symbol_key, chat_id, message_id),
            )

    def set_context_symbol(self, chat_id: int, symbol_key: str) -> None:
        """Record the instrument of the most recently opened trade."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO chat_context(chat_id,symbol_key,updated_utc) VALUES(?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET symbol_key=excluded.symbol_key,updated_utc=excluded.updated_utc""",
                (chat_id, symbol_key, now),
            )

    def context_symbol(self) -> str | None:
        """Instrument of the most recently opened trade across all watched chats."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT symbol_key FROM chat_context ORDER BY updated_utc DESC LIMIT 1").fetchone()
            return str(row[0]) if row else None

    def audit(self, event: dict[str, Any]) -> None:
        row = {"ts_utc": datetime.now(timezone.utc).isoformat(), **event}
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(encoded + "\n")
