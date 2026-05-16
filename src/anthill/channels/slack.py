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

    async def send(self, *, to: str, text: str, reply_to: str | None = None) -> None:
        """`to` is the channel ID (Cxxx) or user ID (Uxxx — must DM-resolve).
        `reply_to` is a thread_ts — threads the reply under that message."""
        payload: dict = {"channel": to, "text": text}
        if reply_to:
            payload["thread_ts"] = reply_to
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
        return ChannelMessage(
            channel="slack",
            sender=channel_id,
            text=text.strip(),
            raw=payload,
            message_id=event.get("ts"),
        )
