"""0.1.61 — Email channel (SMTP send) tests.

The channel uses stdlib smtplib in asyncio.to_thread — no third-party
mail libs to mock around. Tests mock smtplib.SMTP / SMTP_SSL at the
module level to verify:

  - port 587 path uses STARTTLS upgrade
  - port 465 path uses SMTP_SSL (implicit TLS)
  - login is called with the stored credentials
  - send_message is called with a well-formed EmailMessage
  - From: defaults to username when not specified explicitly
  - Subject: derived from first line of text, capped at 78 chars
  - reply_to → In-Reply-To header; thread_id → References header
  - ping() doesn't actually send (no send_message call) — just
    login + QUIT
  - parse_event handles webhook-bridge normalized payloads
"""

from __future__ import annotations

import pytest

from anthill.channels.base import PlatformMessage
from anthill.channels.email import EmailChannel


# --- send() — port + auth + headers --------------------------------------


class _SmtpStub:
    """Fake smtplib.SMTP / SMTP_SSL. Records each method called so the
    test can verify both the connection flow AND header construction."""

    def __init__(self, host=None, port=None, context=None, timeout=None) -> None:
        self.host = host
        self.port = port
        self.context = context
        self.timeout = timeout
        self.calls: list[tuple] = []

    def __enter__(self):
        self.calls.append(("__enter__",))
        return self

    def __exit__(self, *a) -> None:
        self.calls.append(("__exit__",))

    def ehlo(self) -> None:
        self.calls.append(("ehlo",))

    def has_extn(self, name: str) -> bool:
        self.calls.append(("has_extn", name))
        return True  # default: server supports STARTTLS

    def starttls(self, context=None) -> None:
        self.calls.append(("starttls", context is not None))

    def login(self, user, pw) -> None:
        self.calls.append(("login", user, pw))

    def send_message(self, msg) -> None:
        self.calls.append(("send_message", dict(msg.items()), msg.get_content()))


@pytest.mark.asyncio
async def test_send_port_587_uses_starttls(monkeypatch) -> None:
    stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: stub,
    )

    ch = EmailChannel(
        smtp_host="smtp.example.com",
        smtp_port=587,
        username="me@example.com",
        password="pw",
    )
    await ch.send(to="to@example.com", text="hello world")

    methods = [c[0] for c in stub.calls]
    assert "ehlo" in methods
    assert "starttls" in methods
    assert ("login", "me@example.com", "pw") in stub.calls
    # send_message MUST have run.
    sm = [c for c in stub.calls if c[0] == "send_message"]
    assert len(sm) == 1


@pytest.mark.asyncio
async def test_send_port_465_uses_smtp_ssl(monkeypatch) -> None:
    """Port 465 = implicit SSL — bypass STARTTLS upgrade."""
    ssl_stub = _SmtpStub()
    plain_stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP_SSL",
        lambda *a, **kw: ssl_stub,
    )
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: plain_stub,
    )

    ch = EmailChannel(
        smtp_host="smtp.example.com",
        smtp_port=465,
        username="me@example.com",
        password="pw",
    )
    await ch.send(to="to@example.com", text="hello")

    # SMTP_SSL stub had calls; plain SMTP wasn't touched.
    assert any(c[0] == "send_message" for c in ssl_stub.calls)
    assert plain_stub.calls == []
    # And STARTTLS shouldn't have been attempted on the SSL path.
    assert not any(c[0] == "starttls" for c in ssl_stub.calls)


@pytest.mark.asyncio
async def test_send_subject_from_first_line(monkeypatch) -> None:
    stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: stub,
    )
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p"
    )
    await ch.send(
        to="r@example.com",
        text="This is the subject line.\nAnd this is the body.",
    )
    sm = next(c for c in stub.calls if c[0] == "send_message")
    headers = sm[1]
    assert headers["Subject"] == "This is the subject line."


@pytest.mark.asyncio
async def test_send_subject_truncated_at_78(monkeypatch) -> None:
    stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: stub,
    )
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p"
    )
    long_line = "x" * 200
    await ch.send(to="r@example.com", text=long_line)
    sm = next(c for c in stub.calls if c[0] == "send_message")
    assert len(sm[1]["Subject"]) <= 78


@pytest.mark.asyncio
async def test_send_subject_fallback_when_empty(monkeypatch) -> None:
    stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: stub,
    )
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p"
    )
    await ch.send(to="r@example.com", text="\n\n   ")
    sm = next(c for c in stub.calls if c[0] == "send_message")
    assert sm[1]["Subject"] == "Anthill message"


@pytest.mark.asyncio
async def test_send_from_addr_defaults_to_username(monkeypatch) -> None:
    stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: stub,
    )
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="me@example.com", password="p"
    )
    await ch.send(to="r@example.com", text="hi")
    sm = next(c for c in stub.calls if c[0] == "send_message")
    assert sm[1]["From"] == "me@example.com"


@pytest.mark.asyncio
async def test_send_reply_to_sets_in_reply_to(monkeypatch) -> None:
    stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: stub,
    )
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p"
    )
    await ch.send(
        to="r@example.com",
        text="reply body",
        reply_to="<original-msg-id@example.com>",
    )
    sm = next(c for c in stub.calls if c[0] == "send_message")
    assert sm[1]["In-Reply-To"] == "<original-msg-id@example.com>"


@pytest.mark.asyncio
async def test_send_thread_id_sets_references(monkeypatch) -> None:
    stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: stub,
    )
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p"
    )
    await ch.send(
        to="r@example.com",
        text="msg",
        thread_id="<root-msg-id@example.com>",
    )
    sm = next(c for c in stub.calls if c[0] == "send_message")
    assert sm[1]["References"] == "<root-msg-id@example.com>"


# --- ping() — connection check without sending --------------------------


@pytest.mark.asyncio
async def test_ping_returns_true_on_login_success(monkeypatch) -> None:
    stub = _SmtpStub()
    monkeypatch.setattr(
        "anthill.channels.email.smtplib.SMTP",
        lambda *a, **kw: stub,
    )
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p"
    )
    assert await ch.ping() is True
    # Critical: ping must NOT call send_message.
    assert not any(c[0] == "send_message" for c in stub.calls)
    assert any(c[0] == "login" for c in stub.calls)


@pytest.mark.asyncio
async def test_ping_returns_false_on_exception(monkeypatch) -> None:
    """Any auth / network / TLS error → ping returns False, never raises."""
    def raise_smtp(*a, **kw):
        raise ConnectionRefusedError("no server")
    monkeypatch.setattr("anthill.channels.email.smtplib.SMTP", raise_smtp)

    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p"
    )
    assert await ch.ping() is False


# --- parse_event (placeholder for webhook-bridge integration) -----------


def test_parse_event_normalized_webhook_payload() -> None:
    """A Mailgun/Postmark-style normalized inbound mail payload."""
    payload = {
        "from": "user@example.com",
        "body": "Please summarize my last week's standups.",
        "message_id": "<abc@example.com>",
        "in_reply_to": "<root@example.com>",
        "references": "<root@example.com> <middle@example.com>",
    }
    msg = EmailChannel.parse_event(payload)
    assert isinstance(msg, PlatformMessage)
    assert msg.channel == "email"
    assert msg.sender == "user@example.com"
    assert "summarize" in msg.text
    assert msg.message_id == "<abc@example.com>"
    # Thread root = first token of References.
    assert msg.thread_id == "<root@example.com>"
    assert msg.reply_to_id == "<root@example.com>"


def test_parse_event_empty_body_ignored() -> None:
    """Bounces / attachment-only mails get filtered."""
    payload = {"from": "user@example.com", "body": "", "message_id": "<x>"}
    assert EmailChannel.parse_event(payload) is None


def test_parse_event_missing_sender_ignored() -> None:
    payload = {"from": "", "body": "hi", "message_id": "<x>"}
    assert EmailChannel.parse_event(payload) is None


def test_parse_event_thread_id_falls_back_to_in_reply_to() -> None:
    """When References is absent, In-Reply-To becomes the thread key."""
    payload = {
        "from": "user@example.com",
        "body": "ok",
        "message_id": "<m1>",
        "in_reply_to": "<root@example.com>",
    }
    msg = EmailChannel.parse_event(payload)
    assert msg is not None
    assert msg.thread_id == "<root@example.com>"
