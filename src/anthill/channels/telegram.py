"""Telegram channel — send via Bot API, parse Update events.

Telegram has the simplest IM API of the mainstream platforms: a bot
token gives you https://api.telegram.org/bot<token>/<method> with
straight JSON request/response. No tenant tokens, no token refresh.

We support:
    - send (sendMessage)
    - parse Telegram Update payloads from webhook
"""

from __future__ import annotations

import httpx

from anthill.channels.base import Channel, ChannelMessage


TELEGRAM_BASE = "https://api.telegram.org"


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, *, bot_token: str) -> None:
        self.bot_token = bot_token

    @property
    def _api(self) -> str:
        return f"{TELEGRAM_BASE}/bot{self.bot_token}"

    async def send(self, *, to: str, text: str, reply_to: str | None = None) -> None:
        """`to` is the chat_id (numeric string).
        `reply_to` is a message_id to reply to inline."""
        payload: dict = {"chat_id": to, "text": text}
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to)}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self._api}/sendMessage", json=payload)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram send failed: {data}")

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self._api}/getMe")
                response.raise_for_status()
                return response.json().get("ok", False)
        except Exception:
            return False

    @staticmethod
    def parse_event(payload: dict) -> ChannelMessage | None:
        """Parse a Telegram Update into a ChannelMessage.

        Returns None for non-text or non-message updates (edited messages,
        callbacks, etc — supported when there's a real need)."""
        msg = payload.get("message")
        if not msg:
            return None
        text = msg.get("text")
        if not text or not text.strip():
            return None
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None:
            return None
        return ChannelMessage(
            channel="telegram",
            sender=str(chat_id),
            text=text.strip(),
            raw=payload,
            message_id=str(msg.get("message_id")) if msg.get("message_id") else None,
        )
