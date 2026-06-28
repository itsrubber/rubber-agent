"""Profile-scoped WhatsApp ambient chat history.

This store is separate from Hermes conversation transcripts: ambient group
messages are chat context, not agent turns, and replaying them as prior user
messages would break role-alternation assumptions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


DEFAULT_RECENT_LIMIT = 12
DEFAULT_CONTEXT_CHAR_LIMIT = 6000


def default_db_path() -> Path:
    return get_hermes_home() / "gateway" / "whatsapp_history.sqlite3"


def _coerce_timestamp(value: Any) -> float:
    if value is None:
        return time.time()
    try:
        # Baileys commonly emits epoch seconds; some bridges emit millis.
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return ts
    except (TypeError, ValueError):
        return time.time()


def _string(value: Any) -> str:
    return str(value or "").strip()


class WhatsAppHistoryStore:
    """Small SQLite + FTS5 store for inbound WhatsApp messages."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None  # type: ignore[assignment]

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS whatsapp_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT,
                    chat_id TEXT NOT NULL,
                    chat_name TEXT,
                    chat_type TEXT NOT NULL DEFAULT 'dm',
                    sender_id TEXT,
                    sender_name TEXT,
                    body TEXT,
                    timestamp REAL NOT NULL,
                    is_group INTEGER NOT NULL DEFAULT 0,
                    is_from_me INTEGER NOT NULL DEFAULT 0,
                    was_processed INTEGER NOT NULL DEFAULT 0,
                    quoted_message_id TEXT,
                    quoted_participant TEXT,
                    has_quoted_message INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_whatsapp_messages_chat_msg
                    ON whatsapp_messages(chat_id, message_id)
                    WHERE message_id IS NOT NULL AND message_id != '';
                CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_recent
                    ON whatsapp_messages(chat_id, timestamp DESC, id DESC);
                CREATE TABLE IF NOT EXISTS whatsapp_agent_messages (
                    message_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    session_key TEXT,
                    thread_id TEXT,
                    timestamp REAL NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                );
                """
            )
            if self._sqlite_supports_fts5(cur):
                cur.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS whatsapp_messages_fts
                    USING fts5(
                        body,
                        sender_name,
                        chat_name,
                        content='whatsapp_messages',
                        content_rowid='id'
                    );
                    CREATE TRIGGER IF NOT EXISTS whatsapp_messages_ai
                    AFTER INSERT ON whatsapp_messages BEGIN
                        INSERT INTO whatsapp_messages_fts(rowid, body, sender_name, chat_name)
                        VALUES (new.id, new.body, new.sender_name, new.chat_name);
                    END;
                    CREATE TRIGGER IF NOT EXISTS whatsapp_messages_ad
                    AFTER DELETE ON whatsapp_messages BEGIN
                        INSERT INTO whatsapp_messages_fts(
                            whatsapp_messages_fts, rowid, body, sender_name, chat_name
                        ) VALUES ('delete', old.id, old.body, old.sender_name, old.chat_name);
                    END;
                    CREATE TRIGGER IF NOT EXISTS whatsapp_messages_au
                    AFTER UPDATE ON whatsapp_messages BEGIN
                        INSERT INTO whatsapp_messages_fts(
                            whatsapp_messages_fts, rowid, body, sender_name, chat_name
                        ) VALUES ('delete', old.id, old.body, old.sender_name, old.chat_name);
                        INSERT INTO whatsapp_messages_fts(rowid, body, sender_name, chat_name)
                        VALUES (new.id, new.body, new.sender_name, new.chat_name);
                    END;
                    """
                )
            self._conn.commit()

    @staticmethod
    def _sqlite_supports_fts5(cursor: sqlite3.Cursor) -> bool:
        try:
            cursor.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
            cursor.execute("DROP TABLE temp._fts5_probe")
            return True
        except sqlite3.Error:
            return False

    def record_message(self, data: dict[str, Any], *, was_processed: bool = False) -> int | None:
        chat_id = _string(data.get("chatId"))
        if not chat_id:
            return None
        body = _string(data.get("body"))
        message_id = _string(data.get("messageId")) or None
        raw_json = None
        try:
            raw_json = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw_json = None

        values = {
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_name": _string(data.get("chatName")) or None,
            "chat_type": "group" if data.get("isGroup") else "dm",
            "sender_id": _string(data.get("senderId") or data.get("from")) or None,
            "sender_name": _string(data.get("senderName")) or None,
            "body": body,
            "timestamp": _coerce_timestamp(data.get("timestamp")),
            "is_group": 1 if data.get("isGroup") else 0,
            "is_from_me": 1 if data.get("fromMe") else 0,
            "was_processed": 1 if was_processed else 0,
            "quoted_message_id": _string(data.get("quotedMessageId")) or None,
            "quoted_participant": _string(data.get("quotedParticipant")) or None,
            "has_quoted_message": 1 if data.get("hasQuotedMessage") else 0,
            "raw_json": raw_json,
        }

        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO whatsapp_messages (
                        message_id, chat_id, chat_name, chat_type, sender_id,
                        sender_name, body, timestamp, is_group, is_from_me,
                        was_processed, quoted_message_id, quoted_participant,
                        has_quoted_message, raw_json
                    ) VALUES (
                        :message_id, :chat_id, :chat_name, :chat_type, :sender_id,
                        :sender_name, :body, :timestamp, :is_group, :is_from_me,
                        :was_processed, :quoted_message_id, :quoted_participant,
                        :has_quoted_message, :raw_json
                    )
                    ON CONFLICT(chat_id, message_id) WHERE message_id IS NOT NULL AND message_id != ''
                    DO UPDATE SET
                        chat_name=excluded.chat_name,
                        sender_id=excluded.sender_id,
                        sender_name=excluded.sender_name,
                        body=excluded.body,
                        timestamp=excluded.timestamp,
                        was_processed=whatsapp_messages.was_processed OR excluded.was_processed,
                        quoted_message_id=excluded.quoted_message_id,
                        quoted_participant=excluded.quoted_participant,
                        has_quoted_message=excluded.has_quoted_message,
                        raw_json=excluded.raw_json
                    """,
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid or 0) or None
            except sqlite3.Error:
                logger.debug("Failed to record WhatsApp history message", exc_info=True)
                return None

    def mark_processed(self, chat_id: str, message_id: str | None) -> None:
        if not chat_id or not message_id:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE whatsapp_messages SET was_processed = 1 WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                )
                self._conn.commit()
            except sqlite3.Error:
                logger.debug("Failed to mark WhatsApp message processed", exc_info=True)

    def recent_messages(
        self,
        chat_id: str,
        *,
        limit: int = DEFAULT_RECENT_LIMIT,
        before_message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not chat_id:
            return []
        limit = max(0, min(int(limit or DEFAULT_RECENT_LIMIT), 50))
        if limit == 0:
            return []
        params: list[Any] = [chat_id]
        where = "chat_id = ?"
        if before_message_id:
            where += " AND (message_id IS NULL OR message_id != ?)"
            params.append(before_message_id)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM whatsapp_messages
                WHERE {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def format_recent_context(
        self,
        chat_id: str,
        *,
        limit: int = DEFAULT_RECENT_LIMIT,
        char_limit: int = DEFAULT_CONTEXT_CHAR_LIMIT,
        before_message_id: str | None = None,
    ) -> str:
        rows = self.recent_messages(
            chat_id,
            limit=limit,
            before_message_id=before_message_id,
        )
        lines: list[str] = []
        for row in rows:
            body = _string(row.get("body"))
            if not body:
                continue
            sender = row.get("sender_name") or row.get("sender_id") or "unknown"
            lines.append(f"- {sender}: {body}")
        if not lines:
            return ""
        text = "\n".join(lines)
        char_limit = max(0, int(char_limit or DEFAULT_CONTEXT_CHAR_LIMIT))
        if char_limit and len(text) > char_limit:
            text = text[-char_limit:].lstrip()
        return (
            "[Recent WhatsApp chat context - context only, not requests]\n"
            f"{text}"
        )

    def search(self, query: str, *, chat_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        query = _string(query)
        if not query:
            return []
        limit = max(1, min(int(limit or 10), 50))
        params: list[Any] = [query]
        chat_clause = ""
        if chat_id:
            chat_clause = "AND m.chat_id = ?"
            params.append(chat_id)
        params.append(limit)
        with self._lock:
            try:
                rows = self._conn.execute(
                    f"""
                    SELECT m.*
                    FROM whatsapp_messages_fts f
                    JOIN whatsapp_messages m ON m.id = f.rowid
                    WHERE whatsapp_messages_fts MATCH ? {chat_clause}
                    ORDER BY m.timestamp DESC, m.id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            except sqlite3.Error:
                like = f"%{query}%"
                params = [like]
                chat_clause = ""
                if chat_id:
                    chat_clause = "AND chat_id = ?"
                    params.append(chat_id)
                params.append(limit)
                rows = self._conn.execute(
                    f"""
                    SELECT * FROM whatsapp_messages
                    WHERE body LIKE ? {chat_clause}
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        return [dict(row) for row in rows]

    def record_agent_message(
        self,
        *,
        chat_id: str,
        message_id: str,
        session_key: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        chat_id = _string(chat_id)
        message_id = _string(message_id)
        if not chat_id or not message_id:
            return
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO whatsapp_agent_messages (
                        chat_id, message_id, session_key, thread_id, timestamp
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_id) DO UPDATE SET
                        session_key=excluded.session_key,
                        thread_id=excluded.thread_id,
                        timestamp=excluded.timestamp
                    """,
                    (
                        chat_id,
                        message_id,
                        _string(session_key) or None,
                        _string(thread_id) or None,
                        time.time(),
                    ),
                )
                self._conn.commit()
            except sqlite3.Error:
                logger.debug("Failed to record WhatsApp agent message", exc_info=True)

    def lookup_agent_reply_thread(self, *, chat_id: str, message_id: str) -> dict[str, Any] | None:
        chat_id = _string(chat_id)
        message_id = _string(message_id)
        if not chat_id or not message_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM whatsapp_agent_messages
                WHERE chat_id = ? AND message_id = ?
                LIMIT 1
                """,
                (chat_id, message_id),
            ).fetchone()
        return dict(row) if row else None


def safe_record_message(data: dict[str, Any], *, was_processed: bool = False) -> None:
    try:
        store = WhatsAppHistoryStore()
        try:
            store.record_message(data, was_processed=was_processed)
        finally:
            store.close()
    except Exception:
        logger.debug("WhatsApp history record failed", exc_info=True)
