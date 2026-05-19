"""Slack channel — send via Web API, parse Event API payloads.

Auth: bot user OAuth token (xoxb-...).
Endpoints used:
    POST https://slack.com/api/chat.postMessage
    POST https://slack.com/api/auth.test (for ping)
"""

from __future__ import annotations

import httpx

from anthill.channels.base import Channel, ChannelMessage


SLACK_BASE = "https://slack.com/api"


class SlackChannel(Channel):
    name = "slack"

    def __init__(self, *, bot_token: str) -> None:
        self.bot_token = bot_token

    async def send(
        self,
        *,
        to: str,
        text: str,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """`to` is the channel ID (Cxxx) or user ID (Uxxx — must DM-resolve).

        In Slack, `thread_ts` IS the thread identifier — replying to a
        message threads under it. Both reply_to and thread_id work; if
        either is set we set thread_ts. thread_id wins when both given
        (it's the explicit intent).
        """
        payload: dict = {"channel": to, "text": text}
        thread_ts = thread_id or reply_to
        if thread_ts:
            payload["thread_ts"] = thread_ts
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{SLACK_BASE}/chat.postMessage",
                json=payload,
                headers={"Authorization": f"Bearer {self.bot_token}"},
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Slack send failed: {data}")

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{SLACK_BASE}/auth.test",
                    headers={"Authorization": f"Bearer {self.bot_token}"},
                )
                return response.json().get("ok", False)
        except Exception:
            return False

    @staticmethod
    def parse_event(payload: dict) -> ChannelMessage | None:
        """Parse a Slack Events API payload into a ChannelMessage.

        Handles the `event_callback` envelope with an inner `message` event.
        Ignores edited messages, bot messages (echo prevention), and DMs
        from other bots."""
        if payload.get("type") == "url_verification":
            return None  # daemon handles handshake
        event = payload.get("event") or {}
        if event.get("type") != "message":
            return None
        if event.get("subtype"):  # edited/deleted/bot — skip
            return None
        if event.get("bot_id"):
            return None  # do not loop on own messages
        text = event.get("text")
        if not text or not text.strip():
            return None
        channel_id = event.get("channel")
        if not channel_id:
            return None
        # 0.1.57 — Slack's thread_ts is the canonical thread identifier.
        # When user replied INSIDE a thread we get both ts (this msg)
        # and thread_ts (the parent that started the thread).
        return ChannelMessage(
            channel="slack",
            sender=channel_id,
            text=text.strip(),
            raw=payload,
            message_id=event.get("ts"),
            thread_id=event.get("thread_ts"),
        )
