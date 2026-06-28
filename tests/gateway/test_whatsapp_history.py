from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.whatsapp_history import WhatsAppHistoryStore, default_db_path


def _make_adapter(tmp_path, *, require_mention=True):
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    extra = {
        "require_mention": require_mention,
        "history_recent_limit": 5,
        "history_context_char_limit": 2000,
    }
    adapter = object.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._message_handler = AsyncMock()
    adapter._dm_policy = "open"
    adapter._allow_from = set()
    adapter._group_policy = "open"
    adapter._group_allow_from = set()
    adapter._mention_patterns = []
    adapter._reply_prefix = None
    adapter._history_store = WhatsAppHistoryStore(tmp_path / "whatsapp_history.sqlite3")
    return adapter


def _group_message(body="hello", **overrides):
    data = {
        "messageId": "msg-1",
        "timestamp": 1_700_000_000,
        "isGroup": True,
        "body": body,
        "chatId": "120363001234567890@g.us",
        "chatName": "Project chat",
        "senderId": "6281234567890@s.whatsapp.net",
        "senderName": "Ari",
        "mentionedIds": [],
        "botIds": ["15551230000@s.whatsapp.net", "15551230000@lid"],
        "quotedParticipant": "",
        "hasQuotedMessage": False,
        "hasMedia": False,
        "mediaUrls": [],
    }
    data.update(overrides)
    return data


def test_whatsapp_history_uses_profile_scoped_home(monkeypatch, tmp_path):
    hermes_home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    assert default_db_path() == hermes_home / "gateway" / "whatsapp_history.sqlite3"


def test_whatsapp_history_records_and_searches_with_fts(tmp_path):
    store = WhatsAppHistoryStore(tmp_path / "history.sqlite3")
    try:
        store.record_message(
            _group_message(
                "ambient roadmap discussion about vector indexes",
                messageId="ambient-1",
            )
        )

        rows = store.search("roadmap")

        assert rows
        assert rows[0]["message_id"] == "ambient-1"
        assert rows[0]["body"] == "ambient roadmap discussion about vector indexes"
        store.record_agent_message(
            chat_id="120363001234567890@g.us",
            message_id="agent-out-123",
            session_key="agent:main:whatsapp:group:120363001234567890@g.us",
            thread_id="group-thread-7",
        )
        mapped = store.lookup_agent_reply_thread(
            chat_id="120363001234567890@g.us",
            message_id="agent-out-123",
        )
        assert mapped["thread_id"] == "group-thread-7"
    finally:
        store.close()


def test_outbound_agent_messages_without_existing_thread_start_reply_thread(tmp_path):
    adapter = _make_adapter(tmp_path, require_mention=True)
    try:
        adapter._record_whatsapp_agent_message(
            chat_id="120363001234567890@g.us",
            message_id="agent-root-1",
        )

        mapped = adapter._history_store.lookup_agent_reply_thread(
            chat_id="120363001234567890@g.us",
            message_id="agent-root-1",
        )
        assert mapped["thread_id"] == "agent-root-1"
    finally:
        adapter._history_store.close()


@pytest.mark.asyncio
async def test_ignored_ambient_group_message_is_stored_before_gating(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    adapter = _make_adapter(tmp_path, require_mention=True)

    event = await adapter._build_message_event(
        _group_message("ambient note before Hermes is tagged", messageId="ambient-ignored")
    )

    assert event is None
    rows = adapter._history_store.search("ambient", chat_id="120363001234567890@g.us")
    assert [row["message_id"] for row in rows] == ["ambient-ignored"]
    assert rows[0]["was_processed"] == 0
    adapter._history_store.close()


@pytest.mark.asyncio
async def test_tagged_whatsapp_message_includes_recent_chat_context(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    adapter = _make_adapter(tmp_path, require_mention=True)
    await adapter._build_message_event(
        _group_message("ambient context about the launch checklist", messageId="ambient-1")
    )

    event = await adapter._build_message_event(
        _group_message(
            "please summarize",
            messageId="tagged-1",
            mentionedIds=["15551230000@s.whatsapp.net"],
        )
    )

    assert event is not None
    assert event.channel_context is not None
    assert "Recent WhatsApp chat context" in event.channel_context
    assert "ambient context about the launch checklist" in event.channel_context
    assert "please summarize" not in event.channel_context
    rows = adapter._history_store.search("summarize", chat_id="120363001234567890@g.us")
    assert rows[0]["message_id"] == "tagged-1"
    assert rows[0]["was_processed"] == 1
    adapter._history_store.close()


@pytest.mark.asyncio
async def test_reply_to_agent_bypasses_tag_and_sets_reply_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    adapter = _make_adapter(tmp_path, require_mention=True)
    adapter._history_store.record_agent_message(
        chat_id="120363001234567890@g.us",
        message_id="agent-out-123",
        session_key="agent:main:whatsapp:group:120363001234567890@g.us",
        thread_id="group-thread-7",
    )

    event = await adapter._build_message_event(
        _group_message(
            "yes, continue there",
            messageId="reply-1",
            quotedMessageId="agent-out-123",
            quotedParticipant="15551230000@lid",
            quotedText="Previous Hermes answer",
            hasQuotedMessage=True,
        )
    )

    assert event is not None
    assert event.reply_to_message_id == "agent-out-123"
    assert event.reply_to_is_own_message is True
    assert event.reply_to_text == "Previous Hermes answer"
    assert event.source.thread_id == "group-thread-7"
    adapter._history_store.close()


@pytest.mark.asyncio
async def test_reply_to_second_agent_message_uses_actual_quoted_message_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    adapter = _make_adapter(tmp_path, require_mention=True)
    adapter._history_store.record_agent_message(
        chat_id="120363001234567890@g.us",
        message_id="agent-out-first",
        session_key="agent:main:whatsapp:group:120363001234567890@g.us",
        thread_id="first-thread",
    )
    adapter._history_store.record_agent_message(
        chat_id="120363001234567890@g.us",
        message_id="agent-out-second",
        session_key="agent:main:whatsapp:group:120363001234567890@g.us",
        thread_id="second-thread",
    )

    event = await adapter._build_message_event(
        _group_message(
            "continue from that one",
            messageId="reply-to-second",
            quotedMessageId="agent-out-second",
            quotedParticipant="",
            quotedText="Second Hermes answer",
            hasQuotedMessage=True,
        )
    )

    assert event is not None
    assert event.reply_to_message_id == "agent-out-second"
    assert event.reply_to_is_own_message is True
    assert event.source.thread_id == "second-thread"
    adapter._history_store.close()
