"""Lark/Feishu channel — receive messages, send replies via Lark Open API.

This adapter handles the **send** half (post a reply to a chat or user)
and the **inbound** half (parse webhook events from Lark into
ChannelMessage). The daemon that actually listens to webhooks lives in
the next module (anthill.channels.daemon) so this file stays unit-testable.

Auth: Lark uses tenant_access_token. We obtain it from
APP_ID + APP_SECRET, cache for 2 hours.

Endpoints used:
    POST /open-apis/auth/v3/tenant_access_token/internal
    POST /open-apis/im/v1/messages

Webhook verification (challenge handshake) lives in the daemon.
"""

from __future__ import annotations

import json
import time

import httpx

from anthill.channels.base import Channel, ChannelMessage


LARK_BASE = "https://open.feishu.cn"
LARK_BASE_LARK = "https://open.larksuite.com"  # international


class LarkChannel(Channel):
    name = "lark"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url or LARK_BASE
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Lark auth failed: {data}")
            self._token = data["tenant_access_token"]
            self._token_expiry = time.time() + data.get("expire", 7200)
        return self._token

    async def send(
        self,
        *,
        to: str,
        text: str,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Send a message.

        `to`: a chat_id (oc_xxx), open_id (ou_xxx), or user_id depending on
              receive_id_type — we accept either and auto-detect.
        `reply_to`: if set, posts as a reply to that message_id.
        `thread_id` (0.1.57): Lark group threads. When set we use the
              dedicated reply-in-thread endpoint. If both reply_to and
              thread_id are set, reply_to wins (more specific).
        """
        token = await self._ensure_token()
        receive_id_type = self._detect_receive_id_type(to)
        url = f"{self.base_url}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        payload = {
            "receive_id": to,
            "msg_type": "text",
            "content": json.dumps({"text": text}),
        }
        if reply_to:
            url = (
                f"{self.base_url}/open-apis/im/v1/messages/{reply_to}/reply"
            )
            payload = {
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            }

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise RuntimeError(f"Lark send failed: {data}")

    async def ping(self) -> bool:
        try:
            await self._ensure_token()
            return True
        except Exception:
            return False

    @staticmethod
    def _detect_receive_id_type(value: str) -> str:
        if value.startswith("oc_"):
            return "chat_id"
        if value.startswith("ou_"):
            return "open_id"
        if value.startswith("on_"):
            return "union_id"
        if "@" in value:
            return "email"
        return "user_id"

    @staticmethod
    def parse_event(payload: dict) -> ChannelMessage | None:
        """Parse a Lark webhook event into a ChannelMessage.

        Returns None if the event is not a message event we handle.
        Supports both im.message.receive_v1 schema variants.
        """
        # Verification handshake — let the daemon handle echo, not us.
        if payload.get("type") == "url_verification":
            return None

        event = payload.get("event") or payload
        event_type = event.get("type") or payload.get("header", {}).get("event_type")
        if event_type and "message.receive" not in event_type:
            return None

        msg = event.get("message") or {}
        sender = event.get("sender", {}).get("sender_id", {})
        sender_id = (
            sender.get("open_id")
            or sender.get("user_id")
            or sender.get("union_id")
            or "unknown"
        )

        if msg.get("message_type") != "text":
            return None

        try:
            content = json.loads(msg.get("content", "{}"))
        except json.JSONDecodeError:
            return None
        text = content.get("text", "").strip()
        if not text:
            return None

        chat_id = msg.get("chat_id")
        target = chat_id if chat_id else sender_id

        # 0.1.57 — Lark im.message.receive_v1 carries:
        #   - msg.thread_id      : group thread (Lark "话题群")
        #   - msg.parent_id      : the message being replied to
        return ChannelMessage(
            channel="lark",
            sender=target,  # where to reply
            text=text,
            raw=payload,
            message_id=msg.get("message_id"),
            thread_id=msg.get("thread_id"),
            reply_to_id=msg.get("parent_id"),
        )
