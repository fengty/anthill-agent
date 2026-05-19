"""Discord channel — send via Bot API, parse Interaction / Message events.

0.1.60 — fills the largest "tropical channel gap" anthill had vs hermes.
Discord uses bot tokens (different from Telegram's bot tokens — Discord
keys live in the dev portal under "Bot" tab) and a REST endpoint at
https://discord.com/api/v10. Auth header is `Bot <token>` (literal
"Bot " prefix is required; not the OAuth-style "Bearer").

Two delivery modes Discord supports — we use the REST one because
that's the same shape as Telegram/Slack and works without a websocket
gateway connection. The webhook event payload for incoming messages
is what most bot frameworks dispatch.

API surface:
    POST  /channels/{channel_id}/messages   send a message
    GET   /users/@me                        ping / verify token

Thread semantics:
- Discord threads are a CHILD CHANNEL of the parent channel — sending
  a message to a thread is `POST /channels/<thread_channel_id>/messages`
  with no special flag. We treat thread_id as the alternate channel.
- reply_to in Discord is `message_reference`: a quote-reply.

Native event shape we parse:
- MESSAGE_CREATE: { id, content, channel_id, author: {id, bot}, ... }
- referenced_message (when reply): { id }
- channel_id may be a regular channel OR a thread channel.
"""

from __future__ import annotations

import httpx

from anthill.channels.base import Channel, ChannelMessage


DISCORD_BASE = "https://discord.com/api/v10"


class DiscordChannel(Channel):
    name = "discord"

    def __init__(self, *, bot_token: str) -> None:
        self.bot_token = bot_token

    @property
    def _auth(self) -> dict:
        # Discord requires the literal "Bot " prefix. This catches the
        # most common copy-paste bug (using just the token).
        return {"Authorization": f"Bot {self.bot_token}"}

    async def send(
        self,
        *,
        to: str,
        text: str,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """`to` is the channel_id (snowflake numeric string).

        `reply_to`: quote-reply to a specific message_id.
        `thread_id`: when set, post to the thread (which IS a channel
                     in Discord's model — we override `to` with it).
        """
        # Threads are channels in Discord — re-route to the thread channel.
        target_channel = thread_id or to
        payload: dict = {"content": text}
        if reply_to:
            # message_reference is Discord's quote-reply primitive.
            # fail_if_not_exists=false so a deleted parent doesn't kill
            # our reply.
            payload["message_reference"] = {
                "message_id": reply_to,
                "fail_if_not_exists": False,
            }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{DISCORD_BASE}/channels/{target_channel}/messages",
                json=payload,
                headers=self._auth,
            )
            # Discord returns 200 with body on success; bubble the body
            # on error so the user sees Discord's actual rejection
            # reason (rate limit, bad permission, etc.).
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Discord send failed ({response.status_code}): "
                    f"{response.text[:300]}"
                )

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{DISCORD_BASE}/users/@me", headers=self._auth
                )
                # 200 with user object = valid token.
                return response.status_code == 200 and bool(
                    response.json().get("id")
                )
        except Exception:
            return False

    @staticmethod
    def parse_event(payload: dict) -> ChannelMessage | None:
        """Parse a Discord MESSAGE_CREATE event into a ChannelMessage.

        Returns None when:
          - payload isn't a MESSAGE_CREATE
          - the message has no text content (image-only / poll / etc)
          - the message came from a bot (echo-prevention)

        Returns a ChannelMessage with sender=channel_id (where to
        reply) and thread_id set when the channel_id resolves to a
        thread (caller signals this via type=11/12 fields if Discord
        gave them — see Channel Types in API docs).
        """
        # Discord gateway events wrap data under "t" (event type) + "d"
        # (data). Webhook bots see the raw data dict. Accept both.
        if payload.get("t") and payload.get("d") is not None:
            event_type = payload["t"]
            data = payload["d"]
        else:
            event_type = "MESSAGE_CREATE"  # assume direct webhook
            data = payload

        if event_type != "MESSAGE_CREATE":
            return None

        # Echo-prevention: don't react to our own bot's messages.
        author = data.get("author") or {}
        if author.get("bot"):
            return None

        text = (data.get("content") or "").strip()
        if not text:
            return None

        channel_id = data.get("channel_id")
        if not channel_id:
            return None

        # If the channel_id is a thread channel (type 11=public,
        # 12=private), the parent channel can be reached via the
        # message's `member` or via separate API call. For now we
        # report channel_id as both `sender` (where to reply) AND
        # `thread_id` when Discord flagged it as a thread channel.
        # In webhook payloads channel type may not be inline; we
        # fall back to None for thread_id in that case — the next
        # send() back to this channel_id still works correctly
        # because Discord treats thread channels uniformly.
        channel_type = data.get("channel_type")
        thread_id = (
            channel_id if channel_type in (10, 11, 12) else None
        )

        # Quote-reply payload.
        ref = data.get("referenced_message") or {}
        reply_to_id = ref.get("id")

        return ChannelMessage(
            channel="discord",
            sender=channel_id,
            text=text,
            raw=payload,
            message_id=data.get("id"),
            thread_id=thread_id,
            reply_to_id=reply_to_id,
        )
