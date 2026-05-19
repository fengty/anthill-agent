"""Email channel — SMTP send (MVP).

0.1.61 — the second biggest "anyone" gap after Discord. Email is
asynchronous by nature, which makes it the right channel for:
  - long-running background asks that finish hours later
  - cron / scheduled summaries
  - notifications to non-developers who don't use Slack/Telegram

This MVP is **send-only**. Receive (IMAP polling or webhook) is a
follow-up because:
  - SMTP send works with any mail provider out of the box (Gmail
    app passwords, AWS SES, Mailgun, Postmark, corporate Exchange)
  - IMAP receive needs either a polling loop or a webhook bridge
    service, which doubles the integration surface

Implementation:
- stdlib smtplib in `asyncio.to_thread` — zero extra deps
- TLS via STARTTLS by default (port 587). port 465 = implicit SSL.
  port 25 = open relay (rarely available outside dev).

For ping(): connect + login + QUIT without sending. Catches the
most common config bugs (bad creds, wrong host, firewall) without
spamming a test recipient.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from anthill.channels.base import Channel, ChannelMessage


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
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        # If from_addr is omitted, use username — works for most
        # providers where the SMTP user IS the sender mailbox.
        self.from_addr = from_addr or username

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
