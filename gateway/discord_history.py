"""Profile-scoped Discord ambient channel history.

This store is separate from Hermes conversation transcripts: ambient Discord
messages are channel context, not agent turns, and replaying them as prior user
messages would break role-alternation assumptions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


DEFAULT_RECENT_LIMIT = 12
DEFAULT_CONTEXT_CHAR_LIMIT = 6000


def default_db_path() -> Path:
    return get_hermes_home() / "gateway" / "discord_history.sqlite3"


def _string(value: Any) -> str:
    return str(value or "").strip()


def _coerce_timestamp(value: Any) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    try:
        ts = float(value)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return ts
    except (TypeError, ValueError):
        pass
    text = _string(value)
    if text:
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            pass
    return time.time()


def _json_dumps(value: Any) -> str | None:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


class DiscordHistoryStore:
    """Small SQLite + FTS5 store for Discord messages visible to the gateway."""

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
                CREATE TABLE IF NOT EXISTS discord_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT,
                    channel_id TEXT NOT NULL,
                    channel_name TEXT,
                    guild_id TEXT,
                    guild_name TEXT,
                    thread_id TEXT,
                    parent_id TEXT,
                    author_id TEXT,
                    author_name TEXT,
                    display_name TEXT,
                    body TEXT,
                    timestamp REAL NOT NULL,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    was_processed INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT,
                    attachments TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_messages_channel_msg
                    ON discord_messages(channel_id, message_id)
                    WHERE message_id IS NOT NULL AND message_id != '';
                CREATE INDEX IF NOT EXISTS idx_discord_messages_recent
                    ON discord_messages(channel_id, timestamp DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_discord_messages_guild_recent
                    ON discord_messages(guild_id, timestamp DESC, id DESC);
                """
            )
            if self._sqlite_supports_fts5(cur):
                cur.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS discord_messages_fts
                    USING fts5(
                        body,
                        author_name,
                        channel_name,
                        guild_name,
                        content='discord_messages',
                        content_rowid='id'
                    );
                    CREATE TRIGGER IF NOT EXISTS discord_messages_ai
                    AFTER INSERT ON discord_messages BEGIN
                        INSERT INTO discord_messages_fts(rowid, body, author_name, channel_name, guild_name)
                        VALUES (new.id, new.body, new.author_name, new.channel_name, new.guild_name);
                    END;
                    CREATE TRIGGER IF NOT EXISTS discord_messages_ad
                    AFTER DELETE ON discord_messages BEGIN
                        INSERT INTO discord_messages_fts(
                            discord_messages_fts, rowid, body, author_name, channel_name, guild_name
                        ) VALUES ('delete', old.id, old.body, old.author_name, old.channel_name, old.guild_name);
                    END;
                    CREATE TRIGGER IF NOT EXISTS discord_messages_au
                    AFTER UPDATE ON discord_messages BEGIN
                        INSERT INTO discord_messages_fts(
                            discord_messages_fts, rowid, body, author_name, channel_name, guild_name
                        ) VALUES ('delete', old.id, old.body, old.author_name, old.channel_name, old.guild_name);
                        INSERT INTO discord_messages_fts(rowid, body, author_name, channel_name, guild_name)
                        VALUES (new.id, new.body, new.author_name, new.channel_name, new.guild_name);
                    END;
                    """
                )
            self._conn.commit()

    @staticmethod
    def _sqlite_supports_fts5(cursor: sqlite3.Cursor) -> bool:
        try:
            cursor.execute("CREATE VIRTUAL TABLE temp._discord_fts5_probe USING fts5(x)")
            cursor.execute("DROP TABLE temp._discord_fts5_probe")
            return True
        except sqlite3.Error:
            return False

    def record_message(self, data: dict[str, Any], *, was_processed: bool = False) -> int | None:
        channel_id = _string(data.get("channel_id") or data.get("channelId"))
        if not channel_id:
            return None
        message_id = _string(data.get("message_id") or data.get("messageId")) or None
        author_name = _string(data.get("author_name") or data.get("authorName")) or None
        display_name = _string(data.get("display_name") or data.get("displayName")) or None
        attachments = data.get("attachments")

        values = {
            "message_id": message_id,
            "channel_id": channel_id,
            "channel_name": _string(data.get("channel_name") or data.get("channelName")) or None,
            "guild_id": _string(data.get("guild_id") or data.get("guildId")) or None,
            "guild_name": _string(data.get("guild_name") or data.get("guildName")) or None,
            "thread_id": _string(data.get("thread_id") or data.get("threadId")) or None,
            "parent_id": _string(data.get("parent_id") or data.get("parentId")) or None,
            "author_id": _string(data.get("author_id") or data.get("authorId")) or None,
            "author_name": author_name or display_name,
            "display_name": display_name or author_name,
            "body": _string(data.get("body") if "body" in data else data.get("content")),
            "timestamp": _coerce_timestamp(data.get("timestamp") or data.get("created_at")),
            "is_bot": 1 if data.get("is_bot") or data.get("bot") else 0,
            "was_processed": 1 if was_processed else 0,
            "raw_json": data.get("raw_json") if isinstance(data.get("raw_json"), str) else _json_dumps(data.get("raw_json", data)),
            "attachments": attachments if isinstance(attachments, str) else _json_dumps(attachments or []),
        }

        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO discord_messages (
                        message_id, channel_id, channel_name, guild_id, guild_name,
                        thread_id, parent_id, author_id, author_name, display_name,
                        body, timestamp, is_bot, was_processed, raw_json, attachments
                    ) VALUES (
                        :message_id, :channel_id, :channel_name, :guild_id, :guild_name,
                        :thread_id, :parent_id, :author_id, :author_name, :display_name,
                        :body, :timestamp, :is_bot, :was_processed, :raw_json, :attachments
                    )
                    ON CONFLICT(channel_id, message_id) WHERE message_id IS NOT NULL AND message_id != ''
                    DO UPDATE SET
                        channel_name=COALESCE(excluded.channel_name, discord_messages.channel_name),
                        guild_id=COALESCE(excluded.guild_id, discord_messages.guild_id),
                        guild_name=COALESCE(excluded.guild_name, discord_messages.guild_name),
                        thread_id=COALESCE(excluded.thread_id, discord_messages.thread_id),
                        parent_id=COALESCE(excluded.parent_id, discord_messages.parent_id),
                        author_id=COALESCE(excluded.author_id, discord_messages.author_id),
                        author_name=COALESCE(excluded.author_name, discord_messages.author_name),
                        display_name=COALESCE(excluded.display_name, discord_messages.display_name),
                        body=excluded.body,
                        timestamp=excluded.timestamp,
                        is_bot=excluded.is_bot,
                        was_processed=discord_messages.was_processed OR excluded.was_processed,
                        raw_json=excluded.raw_json,
                        attachments=excluded.attachments
                    """,
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid or 0) or None
            except sqlite3.Error:
                logger.debug("Failed to record Discord history message", exc_info=True)
                return None

    def mark_processed(self, channel_id: str, message_id: str | None) -> None:
        if not channel_id or not message_id:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE discord_messages SET was_processed = 1 WHERE channel_id = ? AND message_id = ?",
                    (channel_id, message_id),
                )
                self._conn.commit()
            except sqlite3.Error:
                logger.debug("Failed to mark Discord message processed", exc_info=True)

    def recent_messages(
        self,
        channel_id: str,
        *,
        limit: int = DEFAULT_RECENT_LIMIT,
        before_message_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not channel_id:
            return []
        limit = max(0, min(int(limit or DEFAULT_RECENT_LIMIT), 100))
        if limit == 0:
            return []
        params: list[Any] = [channel_id]
        where = "channel_id = ?"
        if before_message_id:
            where += " AND (message_id IS NULL OR message_id != ?)"
            params.append(before_message_id)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM discord_messages
                WHERE {where}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def format_recent_context(
        self,
        channel_id: str,
        *,
        limit: int = DEFAULT_RECENT_LIMIT,
        char_limit: int = DEFAULT_CONTEXT_CHAR_LIMIT,
        before_message_id: str | None = None,
    ) -> str:
        rows = self.recent_messages(
            channel_id,
            limit=limit,
            before_message_id=before_message_id,
        )
        lines: list[str] = []
        for row in rows:
            body = _string(row.get("body"))
            if not body:
                continue
            name = row.get("display_name") or row.get("author_name") or row.get("author_id") or "unknown"
            suffix = " [bot]" if row.get("is_bot") else ""
            lines.append(f"- {name}{suffix}: {body}")
        if not lines:
            return ""
        text = "\n".join(lines)
        char_limit = max(0, int(char_limit or DEFAULT_CONTEXT_CHAR_LIMIT))
        if char_limit and len(text) > char_limit:
            text = text[-char_limit:].lstrip()
        return (
            "[Recent Discord channel context - context only, not requests]\n"
            f"{text}"
        )

    def search(
        self,
        query: str,
        *,
        channel_id: str | None = None,
        guild_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = _string(query)
        if not query:
            return []
        limit = max(1, min(int(limit or 10), 50))
        clauses: list[str] = []
        params: list[Any] = [query]
        if channel_id:
            clauses.append("m.channel_id = ?")
            params.append(channel_id)
        if guild_id:
            clauses.append("m.guild_id = ?")
            params.append(guild_id)
        extra_where = ("AND " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            try:
                rows = self._conn.execute(
                    f"""
                    SELECT m.*
                    FROM discord_messages_fts f
                    JOIN discord_messages m ON m.id = f.rowid
                    WHERE discord_messages_fts MATCH ? {extra_where}
                    ORDER BY m.timestamp DESC, m.id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            except sqlite3.Error:
                like = f"%{query}%"
                clauses = ["body LIKE ?"]
                params = [like]
                if channel_id:
                    clauses.append("channel_id = ?")
                    params.append(channel_id)
                if guild_id:
                    clauses.append("guild_id = ?")
                    params.append(guild_id)
                params.append(limit)
                rows = self._conn.execute(
                    f"""
                    SELECT * FROM discord_messages
                    WHERE {" AND ".join(clauses)}
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        return [dict(row) for row in rows]


def safe_record_message(data: dict[str, Any], *, was_processed: bool = False) -> None:
    try:
        store = DiscordHistoryStore()
        try:
            store.record_message(data, was_processed=was_processed)
        finally:
            store.close()
    except Exception:
        logger.debug("Discord history record failed", exc_info=True)
