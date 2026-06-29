import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.discord_history import DiscordHistoryStore, default_db_path


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.MessageType = SimpleNamespace(default="default", reply="reply")
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.Object = lambda *, id: SimpleNamespace(id=id)
    discord_mod.Message = type("Message", (), {})
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
from scripts import import_discord_channel_history as importer  # noqa: E402


class FakeDMChannel:
    def __init__(self, channel_id=1, name="dm"):
        self.id = channel_id
        self.name = name


class FakeTextChannel:
    def __init__(self, channel_id=1, name="general", guild_id=9, guild_name="Hermes Server"):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(id=guild_id, name=guild_name)
        self.topic = None

    def history(self, *, limit, before, after=None, oldest_first=None):
        async def _iter():
            return
            yield
        return _iter()


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", FakeDMChannel, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", type("FakeThread", (), {}), raising=False)
    monkeypatch.setattr(discord_platform.discord, "MessageType", SimpleNamespace(default="default", reply="reply"), raising=False)
    monkeypatch.setenv("DISCORD_HISTORY_BACKFILL", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    monkeypatch.delenv("DISCORD_ALLOWED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_IGNORED_CHANNELS", raising=False)
    monkeypatch.delenv("DISCORD_FREE_RESPONSE_CHANNELS", raising=False)

    config = PlatformConfig(enabled=True, token="fake-token")
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999))
    adapter._text_batch_delay_seconds = 0
    adapter._discord_history_store = DiscordHistoryStore(tmp_path / "discord_history.sqlite3")
    adapter.handle_message = AsyncMock()
    return adapter


def make_message(*, channel, content, mentions=None, message_id=123, author=None):
    author = author or SimpleNamespace(id=42, name="ari", display_name="Ari", bot=False)
    return SimpleNamespace(
        id=message_id,
        content=content,
        clean_content=content,
        mentions=list(mentions or []),
        attachments=[],
        message_snapshots=[],
        reference=None,
        created_at=datetime(2026, 6, 29, tzinfo=timezone.utc),
        channel=channel,
        guild=getattr(channel, "guild", None),
        author=author,
        type=discord_platform.discord.MessageType.default,
    )


def test_discord_history_uses_profile_scoped_home(monkeypatch, tmp_path):
    hermes_home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert default_db_path() == hermes_home / "gateway" / "discord_history.sqlite3"


def test_discord_history_records_searches_and_formats_context(tmp_path):
    store = DiscordHistoryStore(tmp_path / "history.sqlite3")
    try:
        store.record_message(
            {
                "message_id": "m1",
                "channel_id": "c1",
                "channel_name": "ops",
                "guild_id": "g1",
                "guild_name": "Rubber",
                "author_id": "u1",
                "author_name": "ari",
                "display_name": "Ari",
                "body": "ambient roadmap discussion about vector indexes",
                "timestamp": "2026-06-29T10:00:00+00:00",
                "attachments": [{"filename": "note.txt", "url": "https://cdn.example/note.txt"}],
            }
        )
        store.mark_processed("c1", "m1")

        rows = store.search("roadmap", channel_id="c1")
        assert rows
        assert rows[0]["message_id"] == "m1"
        assert rows[0]["was_processed"] == 1
        assert "Ari: ambient roadmap discussion" in store.format_recent_context("c1")
    finally:
        store.close()


def test_importer_paginates_and_redacts(monkeypatch):
    calls = []
    pages = [
        [
            {"id": "300", "content": "new token=sk-live-secret123", "timestamp": "2026-06-29T10:02:00+00:00", "author": {"id": "u2", "username": "bob"}},
            {"id": "200", "content": "middle", "timestamp": "2026-06-29T10:01:00+00:00", "author": {"id": "u1", "username": "ari"}},
        ],
        [
            {"id": "100", "content": "old", "timestamp": "2026-06-29T10:00:00+00:00", "author": {"id": "u3", "username": "cam", "bot": True}},
        ],
    ]

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(req, timeout=30):
        calls.append(req.full_url)
        return FakeResponse(pages[len(calls) - 1])

    monkeypatch.setattr(importer.urllib.request, "urlopen", fake_urlopen)

    rows = list(importer.iter_channel_messages(token="bot-token", channel_id="chan", page_limit=2))
    records = [
        importer._message_to_record(row, channel_id="chan", channel_name="general", guild_id="guild")
        for row in rows
    ]

    assert [row["id"] for row in rows] == ["300", "200", "100"]
    assert "before=200" in calls[1]
    assert records[0]["body"] == "new token=[REDACTED]"
    assert records[2]["is_bot"] is True


@pytest.mark.asyncio
async def test_adapter_records_unprocessed_before_mention_gate(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    message = make_message(channel=FakeTextChannel(channel_id=555), content="ambient note")

    adapter._record_discord_history(message, was_processed=False)
    await adapter._handle_message(message)

    adapter.handle_message.assert_not_awaited()
    rows = adapter._discord_history_store.search("ambient", channel_id="555")
    assert [row["message_id"] for row in rows] == ["123"]
    assert rows[0]["was_processed"] == 0
    adapter._discord_history_store.close()


@pytest.mark.asyncio
async def test_adapter_marks_processed_when_dispatching(adapter):
    adapter.config.extra["require_mention"] = False
    message = make_message(channel=FakeTextChannel(channel_id=555), content="process this")

    adapter._record_discord_history(message, was_processed=False)
    await adapter._handle_message(message)

    adapter.handle_message.assert_awaited_once()
    rows = adapter._discord_history_store.search("process", channel_id="555")
    assert rows[0]["message_id"] == "123"
    assert rows[0]["was_processed"] == 1
    adapter._discord_history_store.close()
