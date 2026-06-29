#!/usr/bin/env python3
"""Backfill a Discord channel into Hermes Discord history via REST."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.discord_history import DiscordHistoryStore  # noqa: E402

DISCORD_API_BASE = "https://discord.com/api/v10"

_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret|token|password|passwd|pwd)(\s*[:=]\s*)([^\s,;\'"]+)', re.I),
    re.compile(r"\b(sk-(?:live|test|proj)-[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"\b(rk_(?:live|test)_[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})\b"),
    re.compile(r"\b(mfa\.[A-Za-z0-9_\-]{20,})\b"),
    re.compile(r"\b([MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27})\b"),
    re.compile(r"\b(eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})\b"),
]


def redact_secrets(text: str) -> str:
    redacted = text
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 3:
            redacted = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", redacted)
        else:
            redacted = pat.sub("[REDACTED]", redacted)
    return redacted


def _discord_request(
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = 30,
) -> Any:
    url = f"{DISCORD_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-Agent Discord history importer",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 429:
            try:
                retry_after = float(json.loads(body).get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.1, min(retry_after, 30.0)))
            return _discord_request(method, path, token, params=params, timeout=timeout)
        raise RuntimeError(f"Discord API error {e.code}: {body}") from e


def iter_channel_messages(
    *,
    token: str,
    channel_id: str,
    page_limit: int = 100,
) -> Iterator[dict[str, Any]]:
    before: str | None = None
    effective_limit = max(1, min(int(page_limit), 100))
    while True:
        params = {"limit": str(effective_limit)}
        if before:
            params["before"] = before
        page = _discord_request(
            "GET",
            f"/channels/{channel_id}/messages",
            token,
            params=params,
        )
        if not page:
            return
        for msg in page:
            yield msg
        before = str(page[-1].get("id") or "")
        if not before or len(page) < effective_limit:
            return


def _message_to_record(
    msg: dict[str, Any],
    *,
    channel_id: str,
    channel_name: str | None,
    guild_id: str | None,
) -> dict[str, Any]:
    author = msg.get("author") or {}
    member = msg.get("member") or {}
    display_name = (
        member.get("nick")
        or author.get("global_name")
        or author.get("username")
    )
    content = redact_secrets(msg.get("content") or "")
    attachments = []
    for att in msg.get("attachments") or []:
        clean = dict(att)
        if clean.get("url"):
            clean["url"] = redact_secrets(str(clean["url"]))
        if clean.get("proxy_url"):
            clean["proxy_url"] = redact_secrets(str(clean["proxy_url"]))
        attachments.append(clean)
    return {
        "message_id": str(msg.get("id") or ""),
        "channel_id": channel_id,
        "channel_name": channel_name or channel_id,
        "guild_id": guild_id,
        "guild_name": None,
        "thread_id": str(msg.get("thread_id") or "") or None,
        "parent_id": str(msg.get("parent_id") or "") or None,
        "author_id": str(author.get("id") or "") or None,
        "author_name": author.get("username"),
        "display_name": display_name,
        "body": content,
        "timestamp": msg.get("timestamp"),
        "is_bot": bool(author.get("bot", False)),
        "raw_json": {**msg, "content": content, "attachments": attachments},
        "attachments": attachments,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Discord channel message history into Hermes")
    ap.add_argument("--channel-id", required=True, help="Discord channel ID to backfill")
    ap.add_argument("--channel-name", default=None, help="Optional display name for the channel")
    ap.add_argument("--guild-id", default=None, help="Optional guild/server ID")
    ap.add_argument("--db", type=Path, default=None, help="Override DB path; defaults to $HERMES_HOME/gateway/discord_history.sqlite3")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("error: DISCORD_BOT_TOKEN is required", file=sys.stderr)
        return 2

    rows = list(iter_channel_messages(token=token, channel_id=args.channel_id))
    records = [
        _message_to_record(
            row,
            channel_id=args.channel_id,
            channel_name=args.channel_name,
            guild_id=args.guild_id,
        )
        for row in rows
    ]

    if args.dry_run:
        print(f"would import {len(records)} messages into channel_id={args.channel_id}")
        for r in records[:3]:
            ts = r.get("timestamp") or ""
            try:
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).isoformat(timespec="minutes")
            except ValueError:
                pass
            print(ts, r.get("display_name") or r.get("author_name"), repr((r.get("body") or "")[:120]))
        return 0

    store = DiscordHistoryStore(args.db)
    imported = 0
    try:
        for record in records:
            store.record_message(record, was_processed=False)
            imported += 1
    finally:
        store.close()
    print(f"imported {imported} messages into {args.db or 'default Hermes Discord history DB'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
