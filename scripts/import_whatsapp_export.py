#!/usr/bin/env python3
"""Import a WhatsApp "Export chat" .txt file into Hermes WhatsApp history.

This is for real backfill. WhatsApp/Baileys cannot magically fetch arbitrary old
server history; exported chat text from the phone is the reliable source.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.whatsapp_history import WhatsAppHistoryStore  # noqa: E402

_PATTERNS = [
    # [27/06/2026, 11:20:01] Fabio Roma: hello
    re.compile(r"^\[(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}),\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)(?:\s*(?P<ampm>[AP]M))?\]\s+(?P<rest>.*)$", re.I),
    # 27/06/2026, 11:20 - Fabio Roma: hello
    re.compile(r"^(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4}),\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?)(?:\s*(?P<ampm>[AP]M))?\s+-\s+(?P<rest>.*)$", re.I),
]

_DATE_FORMATS = [
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M:%S", "%d/%m/%y %H:%M",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%y %H:%M:%S", "%m/%d/%y %H:%M",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%y %H:%M:%S", "%d-%m-%y %H:%M",
    "%m-%d-%Y %H:%M:%S", "%m-%d-%Y %H:%M", "%m-%d-%y %H:%M:%S", "%m-%d-%y %H:%M",
    "%d/%m/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M %p", "%d/%m/%y %I:%M:%S %p", "%d/%m/%y %I:%M %p",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%y %I:%M:%S %p", "%m/%d/%y %I:%M %p",
]

_SYSTEM_MARKERS = (
    "Messages and calls are end-to-end encrypted",
    "As mensagens e as chamadas são protegidas",
    "Você criou o grupo",
    "You created group",
    "changed this group's",
    "alterou",
)

_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret|token|password|passwd|pwd)(\s*[:=]\s*)([^\s,;\'\"]+)', re.I),
    re.compile(r"\b(sk-(?:live|test|proj)-[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"\b(rk_(?:live|test)_[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})\b"),
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


def _parse_ts(date_s: str, time_s: str, ampm: str | None) -> float:
    value = f"{date_s} {time_s}" + (f" {ampm.upper()}" if ampm else "")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            pass
    raise ValueError(f"unparseable timestamp: {value}")


def _split_sender(rest: str) -> tuple[str | None, str]:
    # System event: no sender colon.
    if ": " not in rest:
        return None, rest.strip()
    sender, body = rest.split(": ", 1)
    return sender.strip() or None, body.strip()


def iter_export_messages(path: Path) -> Iterator[dict]:
    current: dict | None = None
    bad = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            matched = None
            for pat in _PATTERNS:
                m = pat.match(line)
                if m:
                    matched = m
                    break
            if matched:
                if current:
                    yield current
                try:
                    ts = _parse_ts(matched.group("date"), matched.group("time"), matched.group("ampm"))
                except ValueError:
                    bad += 1
                    current = None
                    continue
                sender, body = _split_sender(matched.group("rest"))
                current = {"timestamp": ts, "senderName": sender, "body": body}
            elif current is not None:
                current["body"] = (current.get("body") or "") + "\n" + line
            elif line.strip():
                bad += 1
    if current:
        yield current
    if bad:
        print(f"warning: skipped {bad} unparseable line(s)", file=sys.stderr)


def stable_message_id(chat_id: str, ts: float, sender: str | None, body: str) -> str:
    h = hashlib.sha256(f"{chat_id}\0{ts:.3f}\0{sender or ''}\0{body}".encode("utf-8")).hexdigest()[:24]
    return f"export-{h}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Import WhatsApp exported .txt chat into Hermes WhatsApp history")
    ap.add_argument("export_txt", type=Path)
    ap.add_argument("--chat-id", required=True, help="WhatsApp group JID, e.g. 120363...@g.us")
    ap.add_argument("--chat-name", default="")
    ap.add_argument("--db", type=Path, default=None, help="Override DB path; defaults to $HERMES_HOME/gateway/whatsapp_history.sqlite3")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = list(iter_export_messages(args.export_txt))
    if args.dry_run:
        print(f"would import {len(rows)} messages into chat_id={args.chat_id}")
        for r in rows[:3]:
            sample_body = redact_secrets(r.get("body") or "")
            print(datetime.fromtimestamp(r["timestamp"]).isoformat(timespec="minutes"), r.get("senderName"), repr(sample_body[:120]))
        return 0

    store = WhatsAppHistoryStore(args.db)
    imported = 0
    try:
        for r in rows:
            body = redact_secrets((r.get("body") or "").strip())
            if not body or any(marker in body for marker in _SYSTEM_MARKERS):
                continue
            sender = r.get("senderName")
            event = {
                "messageId": stable_message_id(args.chat_id, r["timestamp"], sender, body),
                "chatId": args.chat_id,
                "chatName": args.chat_name or args.chat_id,
                "isGroup": args.chat_id.endswith("@g.us"),
                "senderId": sender or "export",
                "senderName": sender or "system",
                "body": body,
                "timestamp": r["timestamp"],
            }
            store.record_message(event, was_processed=False)
            imported += 1
    finally:
        store.close()
    print(f"imported {imported} messages into {args.db or 'default Hermes WhatsApp history DB'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
