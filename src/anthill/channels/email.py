"""Email channel — SMTP send + IMAP receive.

0.1.61 — the second biggest "anyone" gap after Discord. Email is
asynchronous by nature, which makes it the right channel for:
  - long-running background asks that finish hours later
  - cron / scheduled summaries
  - notifications to non-developers who don't use Slack/Telegram

0.1.66 — added IMAP receive: poll for UNSEEN messages, parse them
into PlatformMessage, mark as seen. Two delivery modes both work:
  - polling loop (this module's `fetch_unseen()`) — simplest, works
    with any IMAP server (Gmail, Outlook, Exchange, generic)
  - webhook bridge (existing parse_event hook) — for users on
    Mailgun / Postmark / SES SNS who prefer push delivery

Implementation:
- stdlib smtplib + imaplib in `asyncio.to_thread` — zero extra deps
- TLS via STARTTLS by default (port 587). port 465 = implicit SSL.
- IMAP uses SSL by default (port 993) since most servers require it.

For ping(): connect + login + QUIT without sending. Catches the
most common config bugs (bad creds, wrong host, firewall) without
spamming a test recipient.
"""

from __future__ import annotations

import asyncio
import email as _email
import imaplib
import smtplib
import ssl
from email.message import EmailMessage

from anthill.channels.base import Channel, ChannelMessage


def _decode_part(part) -> str:  # noqa: ANN001 — email.Message has loose typing
    """Decode an email MIME part to str, defensively.

    Email transfer-encoding can be quoted-printable / base64 / 7bit;
    get_payload(decode=True) returns bytes once decoded. Charset
    falls back to utf-8 with errors='replace' so weird encodings
    don't crash the poller.
    """
    try:
        raw = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return ""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, AttributeError):
        return raw.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Cheap HTML → text. Not a parser; we just unwrap tags so the
    LLM downstream sees readable content. Most well-formed mail
    clients send a text/plain alternative; this is the fallback for
    the rare HTML-only senders.
    """
    import re as _re

    # Drop <script>/<style> blocks entirely.
    cleaned = _re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", html,
        flags=_re.DOTALL | _re.IGNORECASE,
    )
    # Replace block tags with newlines for readability.
    cleaned = _re.sub(
        r"</?(p|br|div|li|tr|h\d)[^>]*>", "\n", cleaned, flags=_re.IGNORECASE
    )
    # Strip remaining tags.
    cleaned = _re.sub(r"<[^>]+>", "", cleaned)
    # Collapse whitespace.
    cleaned = _re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


class EmailChannel(Channel):
    name = "email"

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int = 587,
        username: str,
        password: str,
        from_addr: str | None = None,
        imap_host: str | None = None,
        imap_port: int = 993,
        imap_folder: str = "INBOX",
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        # If from_addr is omitted, use username — works for most
        # providers where the SMTP user IS the sender mailbox.
        self.from_addr = from_addr or username
        # 0.1.66 — IMAP receive config. When imap_host is None, the
        # receive path is opt-out (channel is send-only). Most users
        # who turn on receive use the same provider as send, often
        # with `imap.gmail.com` / `outlook.office365.com` etc.
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.imap_folder = imap_folder

    async def send(
        self,
        *,
        to: str,
        text: str,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """`to` is a comma-separated list of email addresses.

        `reply_to`: Message-ID of an email this is replying to —
                    set as In-Reply-To header. Some clients render
                    this as a quote-reply.
        `thread_id`: typically the Message-ID of the thread's root.
                    We set References header for proper threading.
        """
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = to
        # Subject from first 78 chars of text (RFC 2822 line length).
        # Fall back to "Anthill message" for empty/very-long-line input.
        first_line = (text.splitlines() or [""])[0].strip()
        msg["Subject"] = first_line[:78] if first_line else "Anthill message"
        if reply_to:
            msg["In-Reply-To"] = reply_to
        if thread_id:
            # Build References per RFC 2822: root + reply chain.
            # When both fields are set, In-Reply-To MUST be the most
            # recent ancestor (we set it above); References lists
            # everything starting from the root.
            existing_refs = msg.get("References", "")
            if existing_refs:
                msg["References"] = f"{existing_refs} {thread_id}"
            else:
                msg["References"] = thread_id
        msg.set_content(text)

        await asyncio.to_thread(self._send_sync, msg)

    def _send_sync(self, msg: EmailMessage) -> None:
        """Blocking SMTP send. Runs in a thread so the async caller
        doesn't block the event loop on TLS handshake + DATA upload.

        We use STARTTLS on port 587 (the modern default). Port 465
        = implicit SSL (legacy but still common). For port 465 we
        use SMTP_SSL; for everything else we connect plain then
        upgrade via STARTTLS.
        """
        context = ssl.create_default_context()
        if self.smtp_port == 465:
            with smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, context=context, timeout=20
            ) as server:
                server.login(self.username, self.password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as server:
                server.ehlo()
                if server.has_extn("STARTTLS"):
                    server.starttls(context=context)
                    server.ehlo()
                server.login(self.username, self.password)
                server.send_message(msg)

    async def ping(self) -> bool:
        """Connect + login + QUIT. No message is sent. Catches the
        common config bugs (wrong host, bad creds, blocked port) so
        the user knows the channel works before relying on it."""
        try:
            await asyncio.to_thread(self._ping_sync)
            return True
        except Exception:
            return False

    def _ping_sync(self) -> None:
        context = ssl.create_default_context()
        if self.smtp_port == 465:
            with smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, context=context, timeout=10
            ) as server:
                server.login(self.username, self.password)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.ehlo()
                if server.has_extn("STARTTLS"):
                    server.starttls(context=context)
                    server.ehlo()
                server.login(self.username, self.password)

    # ─── 0.1.66 — IMAP receive ─────────────────────────────────────────

    async def fetch_unseen(
        self,
        *,
        limit: int = 20,
        mark_seen: bool = True,
    ) -> list[ChannelMessage]:
        """Poll the IMAP folder for UNSEEN messages, return parsed
        ChannelMessages. Marks each as Seen by default so the next
        poll doesn't re-deliver them.

        Returns [] when imap_host is not configured (channel is
        send-only by design) or on any IMAP error (network down,
        bad creds). Errors are swallowed because polling is meant
        to be run on a timer — a single failed poll shouldn't kill
        the loop.

        `limit` caps per-poll deliveries. Useful when a long-idle
        folder suddenly has 500 unseen messages — better to drain
        in chunks than block the daemon for minutes.
        """
        if not self.imap_host:
            return []
        try:
            return await asyncio.to_thread(
                self._fetch_unseen_sync,
                limit=limit,
                mark_seen=mark_seen,
            )
        except Exception:  # noqa: BLE001 — polling must not crash daemon
            return []

    def _fetch_unseen_sync(
        self, *, limit: int, mark_seen: bool
    ) -> list[ChannelMessage]:
        context = ssl.create_default_context()
        out: list[ChannelMessage] = []
        with imaplib.IMAP4_SSL(
            self.imap_host, self.imap_port, ssl_context=context, timeout=20
        ) as conn:
            conn.login(self.username, self.password)
            conn.select(self.imap_folder)
            # UNSEEN search returns space-separated message UIDs.
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[:limit]
            for uid in uids:
                # PEEK so reading doesn't mark as Seen (we set the
                # flag ourselves only when we successfully parsed).
                typ, fetched = conn.fetch(uid, "(BODY.PEEK[])")
                if typ != "OK" or not fetched:
                    continue
                # imaplib returns nested tuples / bytes; the payload
                # is the second element of the first item.
                raw_msg = None
                for item in fetched:
                    if isinstance(item, tuple) and len(item) >= 2:
                        raw_msg = item[1]
                        break
                if raw_msg is None:
                    continue
                msg = self._parse_rfc822(raw_msg)
                if msg is not None:
                    out.append(msg)
                    if mark_seen:
                        try:
                            conn.store(uid, "+FLAGS", "\\Seen")
                        except Exception:  # noqa: BLE001
                            # Marking failed — the next poll will
                            # redeliver. Annoying but not fatal.
                            pass
        return out

    @staticmethod
    def _parse_rfc822(raw: bytes) -> ChannelMessage | None:
        """Parse a raw RFC 822 message into a ChannelMessage.

        Extracts: From, In-Reply-To, References, Message-ID, body
        (text/plain part preferred; falls back to text/html with
        HTML stripped naively).

        Returns None when the message has no body content (rare
        in practice — most bounces still have a text body).
        """
        try:
            mime = _email.message_from_bytes(raw)
        except Exception:  # noqa: BLE001
            return None

        from_addr = (mime.get("From") or "").strip()
        if not from_addr:
            return None

        # Walk multipart to find a text/plain body. Fall back to
        # text/html with tags stripped.
        body = ""
        if mime.is_multipart():
            for part in mime.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    body = _decode_part(part)
                    if body:
                        break
            if not body:
                for part in mime.walk():
                    if part.get_content_type() == "text/html":
                        html = _decode_part(part)
                        if html:
                            body = _strip_html(html)
                            break
        else:
            body = _decode_part(mime)
            if mime.get_content_type() == "text/html":
                body = _strip_html(body)

        body = (body or "").strip()
        if not body:
            return None

        message_id = mime.get("Message-ID")
        in_reply_to = mime.get("In-Reply-To")
        references = mime.get("References") or ""
        thread_id = (
            references.split()[0]
            if references
            else in_reply_to
        )

        return ChannelMessage(
            channel="email",
            sender=from_addr,
            text=body,
            raw={
                "from": from_addr,
                "body": body,
                "message_id": message_id,
                "in_reply_to": in_reply_to,
                "references": references,
            },
            message_id=message_id,
            thread_id=thread_id,
            reply_to_id=in_reply_to,
        )

    @staticmethod
    def parse_event(payload: dict) -> ChannelMessage | None:
        """Parse an inbound email payload into a ChannelMessage.

        MVP-receive path: when an IMAP poller or a webhook bridge
        (Mailgun / Postmark / SES SNS) hands us a normalized payload
        with `from`, `body`, `message_id`, `in_reply_to`, `references`,
        we surface a ChannelMessage. The IMAP / webhook integration
        is a follow-up; this function is here so other layers can be
        wired against the same parse_event contract every channel
        ships.

        Returns None when the payload has no body — bounces /
        attachment-only mail get filtered.
        """
        from_addr = (payload.get("from") or "").strip()
        body = (payload.get("body") or "").strip()
        if not from_addr or not body:
            return None
        return ChannelMessage(
            channel="email",
            sender=from_addr,
            text=body,
            raw=payload,
            message_id=payload.get("message_id"),
            thread_id=(
                # Use References field root when available, else
                # In-Reply-To. Either gives us a stable thread key.
                (payload.get("references") or "").split()[0]
                if payload.get("references")
                else payload.get("in_reply_to")
            ),
            reply_to_id=payload.get("in_reply_to"),
        )
