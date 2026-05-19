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


# --- 0.1.66 — IMAP receive ----------------------------------------------


def test_parse_rfc822_text_plain() -> None:
    raw = (
        b"From: alice@example.com\r\n"
        b"To: bot@example.com\r\n"
        b"Subject: please analyze\r\n"
        b"Message-ID: <abc@example.com>\r\n"
        b"In-Reply-To: <root@example.com>\r\n"
        b"References: <root@example.com> <middle@example.com>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"Please summarize my standups\r\n"
    )
    msg = EmailChannel._parse_rfc822(raw)
    assert msg is not None
    assert msg.sender == "alice@example.com"
    assert "summarize" in msg.text
    assert msg.message_id == "<abc@example.com>"
    assert msg.thread_id == "<root@example.com>"
    assert msg.reply_to_id == "<root@example.com>"


def test_parse_rfc822_html_fallback() -> None:
    """Pure HTML mail → tags stripped, text usable."""
    raw = (
        b"From: alice@example.com\r\n"
        b"Subject: t\r\n"
        b"Message-ID: <x>\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><p>Hello <b>world</b></p>"
        b"<script>alert(1)</script>"
        b"<p>second paragraph</p></body></html>\r\n"
    )
    msg = EmailChannel._parse_rfc822(raw)
    assert msg is not None
    assert "Hello" in msg.text
    assert "world" in msg.text
    assert "alert" not in msg.text  # script stripped
    assert "<p>" not in msg.text


def test_parse_rfc822_multipart_prefers_text_plain() -> None:
    raw = (
        b"From: alice@example.com\r\n"
        b"Subject: t\r\n"
        b"Message-ID: <x>\r\n"
        b"Content-Type: multipart/alternative; boundary=BOUNDARY\r\n"
        b"\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"PLAIN VERSION\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<p>HTML VERSION</p>\r\n"
        b"--BOUNDARY--\r\n"
    )
    msg = EmailChannel._parse_rfc822(raw)
    assert msg is not None
    assert msg.text == "PLAIN VERSION"


def test_parse_rfc822_no_sender_returns_none() -> None:
    raw = b"Subject: anon\r\n\r\nhello\r\n"
    assert EmailChannel._parse_rfc822(raw) is None


def test_parse_rfc822_no_body_returns_none() -> None:
    raw = (
        b"From: alice@example.com\r\n"
        b"Subject: t\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"   \r\n"
    )
    assert EmailChannel._parse_rfc822(raw) is None


def test_parse_rfc822_invalid_bytes_returns_none() -> None:
    """Garbage in → None out, no exception."""
    # `message_from_bytes` is very permissive — it accepts almost
    # anything. We pass deliberately empty bytes to force None.
    assert EmailChannel._parse_rfc822(b"") is None


@pytest.mark.asyncio
async def test_fetch_unseen_returns_empty_when_no_imap_host() -> None:
    """Send-only channel → fetch_unseen no-ops."""
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p"
    )
    assert ch.imap_host is None
    assert await ch.fetch_unseen() == []


@pytest.mark.asyncio
async def test_fetch_unseen_swallows_imap_errors(monkeypatch) -> None:
    """Network error / bad creds → return [], never raise."""
    import imaplib

    def boom(*a, **kw):
        raise OSError("imap down")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", boom)
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p",
        imap_host="imap.example.com",
    )
    assert await ch.fetch_unseen() == []


@pytest.mark.asyncio
async def test_fetch_unseen_parses_and_marks_seen(monkeypatch) -> None:
    """Successful poll: returns parsed messages and marks each Seen."""
    import imaplib

    raw_msg = (
        b"From: alice@example.com\r\n"
        b"Message-ID: <abc@example.com>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"hello from inbox\r\n"
    )

    stored: list[tuple] = []

    class _ImapStub:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, *a, **kw):
            pass

        def select(self, *a, **kw):
            pass

        def search(self, _, criterion):
            return ("OK", [b"42 43"])

        def fetch(self, uid, _query):
            return ("OK", [(b"42 (BODY[])", raw_msg)])

        def store(self, uid, op, flags):
            stored.append((uid, op, flags))

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _ImapStub)
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p",
        imap_host="imap.example.com",
    )
    msgs = await ch.fetch_unseen()
    # 2 unseen UIDs returned by search; each fetch returns the same
    # body (stub) → 2 parsed messages, 2 Seen marks.
    assert len(msgs) == 2
    assert all(m.sender == "alice@example.com" for m in msgs)
    assert len(stored) == 2
    assert all(op == "+FLAGS" and flags == "\\Seen" for _, op, flags in stored)


@pytest.mark.asyncio
async def test_fetch_unseen_respects_mark_seen_false(monkeypatch) -> None:
    """When mark_seen=False, the IMAP store call is skipped — useful
    for dry-run / preview modes."""
    import imaplib

    raw_msg = (
        b"From: alice@example.com\r\nMessage-ID: <x>\r\n"
        b"Content-Type: text/plain\r\n\r\nbody\r\n"
    )

    stored: list = []

    class _ImapStub:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def login(self, *a, **kw):
            pass

        def select(self, *a, **kw):
            pass

        def search(self, *a, **kw):
            return ("OK", [b"1"])

        def fetch(self, uid, _q):
            return ("OK", [(b"1 (BODY[])", raw_msg)])

        def store(self, *a, **kw):
            stored.append(a)

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _ImapStub)
    ch = EmailChannel(
        smtp_host="x", smtp_port=587, username="u", password="p",
        imap_host="imap.example.com",
    )
    msgs = await ch.fetch_unseen(mark_seen=False)
    assert len(msgs) == 1
    assert stored == []  # store never called
