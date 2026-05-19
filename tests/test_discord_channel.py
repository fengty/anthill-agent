"""0.1.60 — Discord channel adapter tests.

Verifies parse_event handles:
  - direct webhook envelope (raw data dict)
  - gateway-style envelope (t + d)
  - bot echo prevention
  - empty content / image-only messages → None
  - quote-reply (referenced_message → reply_to_id)

And send() smoke:
  - Auth header carries literal "Bot " prefix
  - thread_id re-routes the POST to that channel (Discord threads
    are channels)
  - reply_to sets message_reference
"""

from __future__ import annotations

import pytest

from anthill.channels.base import PlatformMessage
from anthill.channels.discord import DiscordChannel


# --- parse_event ---------------------------------------------------------


def test_parse_event_direct_envelope() -> None:
    """Webhook bots see the raw data dict (no t/d wrapper)."""
    payload = {
        "id": "msg-123",
        "channel_id": "ch-1",
        "content": "hi there",
        "author": {"id": "user-1", "bot": False},
    }
    msg = DiscordChannel.parse_event(payload)
    assert isinstance(msg, PlatformMessage)
    assert msg.channel == "discord"
    assert msg.sender == "ch-1"
    assert msg.text == "hi there"
    assert msg.message_id == "msg-123"
    assert msg.thread_id is None
    assert msg.reply_to_id is None


def test_parse_event_gateway_envelope() -> None:
    """Gateway pushes wrap data in t (event type) + d (payload)."""
    payload = {
        "t": "MESSAGE_CREATE",
        "d": {
            "id": "msg-456",
            "channel_id": "ch-2",
            "content": "via gateway",
            "author": {"id": "user-2", "bot": False},
        },
    }
    msg = DiscordChannel.parse_event(payload)
    assert msg is not None
    assert msg.text == "via gateway"
    assert msg.message_id == "msg-456"


def test_parse_event_bot_messages_ignored() -> None:
    """Echo-prevention: don't react to our own messages."""
    payload = {
        "id": "msg-1",
        "channel_id": "ch-1",
        "content": "i'm a bot speaking",
        "author": {"id": "bot-self", "bot": True},
    }
    assert DiscordChannel.parse_event(payload) is None


def test_parse_event_empty_content_ignored() -> None:
    """Image-only / sticker-only / poll messages have no .content text.
    Don't try to process them — model can't see the image."""
    payload = {
        "id": "msg-1",
        "channel_id": "ch-1",
        "content": "",
        "author": {"id": "u", "bot": False},
    }
    assert DiscordChannel.parse_event(payload) is None


def test_parse_event_wrong_event_type_ignored() -> None:
    payload = {
        "t": "TYPING_START",
        "d": {"channel_id": "ch-1"},
    }
    assert DiscordChannel.parse_event(payload) is None


def test_parse_event_thread_channel_sets_thread_id() -> None:
    """When the channel type indicates a thread channel (10/11/12),
    surface it as thread_id so subsequent send() back to it preserves
    the threading."""
    payload = {
        "id": "msg-1",
        "channel_id": "thread-channel-id",
        "channel_type": 11,  # public thread
        "content": "in thread",
        "author": {"id": "u", "bot": False},
    }
    msg = DiscordChannel.parse_event(payload)
    assert msg is not None
    assert msg.thread_id == "thread-channel-id"


def test_parse_event_quote_reply_sets_reply_to() -> None:
    payload = {
        "id": "msg-2",
        "channel_id": "ch-1",
        "content": "replying",
        "author": {"id": "u", "bot": False},
        "referenced_message": {"id": "msg-1"},
    }
    msg = DiscordChannel.parse_event(payload)
    assert msg is not None
    assert msg.reply_to_id == "msg-1"


# --- send() smoke --------------------------------------------------------


@pytest.mark.asyncio
async def test_send_uses_bot_prefix_in_auth(monkeypatch) -> None:
    """Discord auth header is `Bot <token>` — different from
    OAuth's `Bearer`. Easy copy-paste bug; verify the prefix."""
    import httpx

    captured: dict = {}

    class _StubResp:
        status_code = 200
        text = ""

    class _StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _StubResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _StubClient())

    ch = DiscordChannel(bot_token="raw-token")
    await ch.send(to="123", text="hi")
    assert captured["headers"]["Authorization"] == "Bot raw-token"
    assert "/channels/123/messages" in captured["url"]


@pytest.mark.asyncio
async def test_send_routes_to_thread_id(monkeypatch) -> None:
    """thread_id overrides `to` because Discord threads are channels."""
    import httpx

    captured: dict = {}

    class _StubResp:
        status_code = 200
        text = ""

    class _StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json, headers):
            captured["url"] = url
            return _StubResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _StubClient())

    ch = DiscordChannel(bot_token="x")
    await ch.send(to="parent-channel", text="t", thread_id="thread-channel")
    assert "/channels/thread-channel/messages" in captured["url"]
    assert "parent-channel" not in captured["url"]


@pytest.mark.asyncio
async def test_send_reply_to_sets_message_reference(monkeypatch) -> None:
    """Quote-reply uses message_reference with fail_if_not_exists=false."""
    import httpx

    captured: dict = {}

    class _StubResp:
        status_code = 200
        text = ""

    class _StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, json, headers):
            captured["json"] = json
            return _StubResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _StubClient())

    ch = DiscordChannel(bot_token="x")
    await ch.send(to="ch-1", text="reply", reply_to="msg-99")
    assert captured["json"]["message_reference"]["message_id"] == "msg-99"
    # fail_if_not_exists=false so deleted parent doesn't kill our reply.
    assert captured["json"]["message_reference"]["fail_if_not_exists"] is False


@pytest.mark.asyncio
async def test_send_raises_on_http_error(monkeypatch) -> None:
    """Bubble Discord's actual error body so the user can see the
    rate-limit or permission reason — not just a generic 4xx."""
    import httpx

    class _ErrResp:
        status_code = 429
        text = '{"message":"You are being rate limited.","retry_after":1.0}'

    class _StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return _ErrResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _StubClient())

    ch = DiscordChannel(bot_token="x")
    with pytest.raises(RuntimeError) as excinfo:
        await ch.send(to="ch-1", text="hi")
    assert "429" in str(excinfo.value)
    assert "rate limited" in str(excinfo.value).lower()
