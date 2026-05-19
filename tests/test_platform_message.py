"""0.1.57 — PlatformMessage upgrade.

Verifies:
  - PlatformMessage retains backward-compat with ChannelMessage alias
  - new fields default to safe values
  - source defaults to channel name when not given
  - each existing channel's parse_event populates thread/reply when the
    platform's payload carries it (and leaves None when it doesn't)
  - each channel's send() accepts the new thread_id kwarg without
    crashing (smoke test — actual send is mocked)
"""

from __future__ import annotations

import pytest

from anthill.channels.base import (
    Channel,
    ChannelMessage,
    MediaAttachment,
    PlatformMessage,
)


# --- PlatformMessage shape -----------------------------------------------


def test_channel_message_is_alias_for_platform_message() -> None:
    """Old code that imports ChannelMessage still works."""
    assert ChannelMessage is PlatformMessage


def test_platform_message_minimal_construction() -> None:
    msg = PlatformMessage(channel="slack", sender="C123", text="hi")
    assert msg.channel == "slack"
    assert msg.thread_id is None
    assert msg.reply_to_id is None
    assert msg.media == []
    # source defaults to channel name.
    assert msg.source == "slack"


def test_platform_message_explicit_source_overrides_default() -> None:
    msg = PlatformMessage(
        channel="slack", sender="C123", text="hi", source="mcp-proxy"
    )
    assert msg.source == "mcp-proxy"


def test_media_attachment_default_no_transcript() -> None:
    m = MediaAttachment(kind="audio", data="/tmp/voice.ogg", mime="audio/ogg")
    assert m.transcript is None


def test_channel_abstract_has_new_send_signature() -> None:
    """Anyone subclassing Channel must accept thread_id kwarg."""
    import inspect
    sig = inspect.signature(Channel.send)
    assert "thread_id" in sig.parameters


# --- Per-channel parse_event populates thread/reply ----------------------


def test_telegram_parse_populates_thread_and_reply() -> None:
    from anthill.channels.telegram import TelegramChannel

    payload = {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "chat": {"id": -100123},
            "text": "in topic",
            "message_thread_id": 7,
            "reply_to_message": {"message_id": 41},
        },
    }
    msg = TelegramChannel.parse_event(payload)
    assert msg is not None
    assert msg.thread_id == "7"
    assert msg.reply_to_id == "41"
    assert msg.message_id == "42"


def test_telegram_parse_without_thread_leaves_none() -> None:
    from anthill.channels.telegram import TelegramChannel

    payload = {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "chat": {"id": 1},
            "text": "plain dm",
        },
    }
    msg = TelegramChannel.parse_event(payload)
    assert msg is not None
    assert msg.thread_id is None
    assert msg.reply_to_id is None


def test_slack_parse_populates_thread_ts() -> None:
    from anthill.channels.slack import SlackChannel

    payload = {
        "event": {
            "type": "message",
            "channel": "C123",
            "text": "reply",
            "ts": "1700000000.000100",
            "thread_ts": "1700000000.000001",
        }
    }
    msg = SlackChannel.parse_event(payload)
    assert msg is not None
    assert msg.thread_id == "1700000000.000001"
    assert msg.message_id == "1700000000.000100"


def test_slack_parse_top_level_message_no_thread() -> None:
    from anthill.channels.slack import SlackChannel

    payload = {
        "event": {
            "type": "message",
            "channel": "C123",
            "text": "top-level",
            "ts": "1700000000.000100",
        }
    }
    msg = SlackChannel.parse_event(payload)
    assert msg is not None
    assert msg.thread_id is None


def test_lark_parse_populates_thread_and_parent() -> None:
    from anthill.channels.lark import LarkChannel

    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {
                "message_id": "om_xyz",
                "chat_id": "oc_chat",
                "message_type": "text",
                "content": '{"text": "在话题里"}',
                "thread_id": "thread_42",
                "parent_id": "om_parent",
            },
        },
    }
    msg = LarkChannel.parse_event(payload)
    assert msg is not None
    assert msg.thread_id == "thread_42"
    assert msg.reply_to_id == "om_parent"


def test_lark_parse_main_chat_leaves_thread_none() -> None:
    from anthill.channels.lark import LarkChannel

    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {
                "message_id": "om_xyz",
                "chat_id": "oc_chat",
                "message_type": "text",
                "content": '{"text": "plain"}',
            },
        },
    }
    msg = LarkChannel.parse_event(payload)
    assert msg is not None
    assert msg.thread_id is None
    assert msg.reply_to_id is None


# --- send() signature smoke tests ---------------------------------------


@pytest.mark.asyncio
async def test_telegram_send_accepts_thread_id(monkeypatch) -> None:
    """Smoke: thread_id flows through to message_thread_id in payload."""
    import httpx

    from anthill.channels.telegram import TelegramChannel

    captured: dict = {}

    class _StubResp:
        def __init__(self):
            self._data = {"ok": True}

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class _StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return _StubResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _StubClient())

    ch = TelegramChannel(bot_token="test")
    await ch.send(to="123", text="hi", thread_id="7", reply_to="41")
    assert captured["json"]["message_thread_id"] == 7
    assert captured["json"]["reply_parameters"] == {"message_id": 41}


@pytest.mark.asyncio
async def test_wecom_send_accepts_thread_id_but_ignores(monkeypatch) -> None:
    """WeCom has no thread concept — thread_id must be silently accepted."""
    import httpx

    from anthill.channels.wecom import WeComChannel

    class _StubResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"errcode": 0}

    class _StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return _StubResp()

        async def get(self, *a, **kw):
            return _StubResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _StubClient())

    ch = WeComChannel(corp_id="x", corp_secret="y", agent_id="z")
    # Pre-populate token cache so we skip the get-token network step.
    ch._token = "fake"
    ch._token_expiry = 9999999999.0
    # Just verify the kwarg is accepted; no exception expected.
    await ch.send(to="ZhangSan", text="hi", thread_id="ignored")
