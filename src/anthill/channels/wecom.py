"""WeCom (企业微信 / WeChat Work) channel.

Auth: corp_id + corp_secret (one per agent app) -> access_token cached
for 2 hours.

Send: POST /cgi-bin/message/send

Inbound: WeCom sends encrypted XML callbacks. Our parse_event accepts
the *already-decrypted* event dict (a corporate proxy or wxcrypt
helper does the decryption); we focus on event shape, not crypto. A
later release can add wxcrypt support if there's demand.

Docs:
    https://developer.work.weixin.qq.com/document/path/90664  (auth)
    https://developer.work.weixin.qq.com/document/path/90236  (message send)
"""

from __future__ import annotations

import time

import httpx

from anthill.channels.base import Channel, ChannelMessage


WECOM_BASE = "https://qyapi.weixin.qq.com"


class WeComChannel(Channel):
    name = "wecom"

    def __init__(
        self,
        *,
        corp_id: str,
        corp_secret: str,
        agent_id: int,
    ) -> None:
        self.corp_id = corp_id
        self.corp_secret = corp_secret
        self.agent_id = agent_id
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{WECOM_BASE}/cgi-bin/gettoken",
                params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
            )
            response.raise_for_status()
            data = response.json()
            if data.get("errcode") != 0:
                raise RuntimeError(f"WeCom auth failed: {data}")
            self._token = data["access_token"]
            self._token_expiry = time.time() + data.get("expires_in", 7200)
        return self._token

    async def send(
        self,
        *,
        to: str,
        text: str,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Send a text message.

        `to` is one of:
            - userid (default)         e.g. "ZhangSan"
            - userid|userid|...        multiple users
            - "@all"                   broadcast (use carefully)

        Both `reply_to` and `thread_id` are ignored — WeCom messages
        don't thread or quote. The kwargs exist for ABC signature
        match across channels.
        """
        token = await self._ensure_token()
        payload = {
            "touser": to,
            "msgtype": "text",
            "agentid": self.agent_id,
            "text": {"content": text},
            "safe": 0,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{WECOM_BASE}/cgi-bin/message/send",
                params={"access_token": token},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("errcode") != 0:
                raise RuntimeError(f"WeCom send failed: {data}")

    async def ping(self) -> bool:
        try:
            await self._ensure_token()
            return True
        except Exception:
            return False

    @staticmethod
    def parse_event(payload: dict) -> ChannelMessage | None:
        """Parse a *decrypted* WeCom inbound event into a ChannelMessage.

        Expected shape (already XML-decoded into a dict):
            {
              "MsgType": "text",
              "Content": "hello",
              "FromUserName": "ZhangSan",
              "ToUserName": "wxAgent",
              "MsgId": "1234567890",
              "AgentID": 1000002,
              ...
            }

        Returns None for non-text events or empty content. Crypto handshake
        verification lives in the daemon, not here.
        """
        msg_type = payload.get("MsgType") or payload.get("msgtype")
        if msg_type and msg_type != "text":
            return None
        content = (payload.get("Content") or payload.get("content") or "").strip()
        if not content:
            return None
        from_user = (
            payload.get("FromUserName") or payload.get("fromusername") or "unknown"
        )
        msg_id = payload.get("MsgId") or payload.get("msgid")
        return ChannelMessage(
            channel="wecom",
            sender=str(from_user),
            text=content,
            raw=payload,
            message_id=str(msg_id) if msg_id is not None else None,
        )
