"""HTTP daemon — listens for IM webhooks and dispatches them to the nation.

`anthill serve` starts a FastAPI server that:
    1. Receives webhook POST from Lark (or another channel)
    2. Verifies the challenge handshake when applicable
    3. Parses the payload into a ChannelMessage
    4. Pipes the text through Nation.ask
    5. Posts the result back to the channel

Why FastAPI/uvicorn: standard, async-native, the smallest hop from
'webhook URL' to 'production-ready listener.' Listed as an optional
dependency (`pip install anthill-agent[daemon]`) so users who only
want the CLI don't pay the install cost.

We keep the route handlers tiny — every route delegates to a small
async function with no FastAPI dep so the logic can be tested
directly (test_daemon).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

# FastAPI types need to be resolvable from this module's globals for
# FastAPI's get_type_hints() inspection. Importing under a guard so the
# rest of Anthill still works when the [daemon] extras are absent.
try:
    from fastapi import FastAPI as _FastAPI, Request as _Request  # noqa: F401
except ImportError:  # pragma: no cover
    _FastAPI = None  # type: ignore[assignment]
    _Request = None  # type: ignore[assignment]

from anthill.channels.base import Channel, ChannelMessage
from anthill.channels.lark import LarkChannel
from anthill.channels.slack import SlackChannel
from anthill.channels.telegram import TelegramChannel
from anthill.channels.wecom import WeComChannel
from anthill.config import AnthillConfig
from anthill.core.feedback import AskRecord, save_last_ask
from anthill.core.history import append_history, build_entry_from_ask
from anthill.core.nation import Nation
from anthill.core.persistence import load_nation, nation_dir, save_nation
from anthill.core.router import RouterConfig


log = logging.getLogger("anthill.daemon")


@dataclass
class DaemonConfig:
    """Daemon settings, sourced from env vars."""

    nation_name: str = "default"
    host: str = "0.0.0.0"
    port: int = 8765
    lark_app_id: str | None = None
    lark_app_secret: str | None = None
    lark_verification_token: str | None = None
    telegram_bot_token: str | None = None
    slack_bot_token: str | None = None
    wecom_corp_id: str | None = None
    wecom_corp_secret: str | None = None
    wecom_agent_id: int | None = None

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        wecom_agent_raw = os.getenv("ANTHILL_WECOM_AGENT_ID")
        return cls(
            nation_name=os.getenv("ANTHILL_DAEMON_NATION", "default"),
            host=os.getenv("ANTHILL_DAEMON_HOST", "0.0.0.0"),
            port=int(os.getenv("ANTHILL_DAEMON_PORT", "8765")),
            lark_app_id=os.getenv("ANTHILL_LARK_APP_ID"),
            lark_app_secret=os.getenv("ANTHILL_LARK_APP_SECRET"),
            lark_verification_token=os.getenv("ANTHILL_LARK_VERIFICATION_TOKEN"),
            telegram_bot_token=os.getenv("ANTHILL_TELEGRAM_BOT_TOKEN"),
            slack_bot_token=os.getenv("ANTHILL_SLACK_BOT_TOKEN"),
            wecom_corp_id=os.getenv("ANTHILL_WECOM_CORP_ID"),
            wecom_corp_secret=os.getenv("ANTHILL_WECOM_CORP_SECRET"),
            wecom_agent_id=int(wecom_agent_raw) if wecom_agent_raw else None,
        )


def _load_or_create_nation(config: AnthillConfig, name: str) -> Nation:
    nation = load_nation(name, config.home)
    if nation is None:
        nation = Nation(
            name=name,
            router_config=RouterConfig(exploration=config.exploration_rate),
        )
        if not nation.agents:
            nation.spawn(count=3, model=config.default_model)
        save_nation(nation, config.home)
    return nation


async def handle_message(
    msg: ChannelMessage,
    nation: Nation,
    channel: Channel,
    config: AnthillConfig,
) -> None:
    """Run an inbound message through the nation, post the reply back."""
    log.info("inbound %s from %s: %r", msg.channel, msg.sender, msg.text[:60])
    result = await nation.ask(msg.text)
    save_nation(nation, config.home)

    # Persist last_ask for /rate to target, plus history.
    pairs = [
        (o.final.agent_id, o.subtask.task_type)
        for o in result.outcomes
        if o.status == "ok" and o.final is not None
    ]
    if pairs:
        save_last_ask(
            AskRecord(
                request=msg.text,
                timestamp=time.time(),
                pairs=pairs,
                final_output=result.final_output,
            ),
            nation_dir(config.home, nation.name),
        )
    append_history(
        build_entry_from_ask(msg.text, result.plan.subtasks, result.outcomes),
        nation_dir(config.home, nation.name),
    )

    reply = result.final_output if result.final_output.strip() else "(no answer)"
    try:
        await channel.send(to=msg.sender, text=reply, reply_to=msg.message_id)
    except Exception as e:  # noqa: BLE001
        log.error("failed to send reply: %s", e)


def build_app(daemon_config: DaemonConfig | None = None):
    """Construct the FastAPI app.

    Importing FastAPI here keeps the cold start tiny when the daemon is
    not used, and lets `pip install anthill-agent` succeed without the
    daemon extras.
    """
    if _FastAPI is None or _Request is None:
        raise RuntimeError(
            "Daemon mode requires the [daemon] extras. "
            "Install with: pip install 'anthill-agent[daemon]'"
        )
    FastAPI = _FastAPI  # local alias for readability below
    # Note: _Request is referenced in the route type annotations directly,
    # so it must stay at module scope where FastAPI's get_type_hints sees it.

    daemon = daemon_config or DaemonConfig.from_env()
    config = AnthillConfig.load()
    config.ensure_home()
    nation = _load_or_create_nation(config, daemon.nation_name)

    # Build channels for whichever credentials are present. Priority:
    # 1) anything configured via `anthill channel add` (UserConfig)
    # 2) anything still set via env vars (backward-compat path)
    lark: LarkChannel | None = None
    telegram: TelegramChannel | None = None
    slack: SlackChannel | None = None
    wecom: WeComChannel | None = None

    try:
        from anthill.cli.channel_cmd import build_channel
        from anthill.core.userconfig import load_config as _load_user_cfg
        for entry in _load_user_cfg().channels:
            built = build_channel(entry)
            if built is None:
                continue
            if entry.kind == "lark" and lark is None:
                lark = built  # type: ignore[assignment]
            elif entry.kind == "telegram" and telegram is None:
                telegram = built  # type: ignore[assignment]
            elif entry.kind == "slack" and slack is None:
                slack = built  # type: ignore[assignment]
            elif entry.kind == "wecom" and wecom is None:
                wecom = built  # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        # Config layer is best-effort; env-var fallback below still runs.
        pass

    if lark is None and daemon.lark_app_id and daemon.lark_app_secret:
        lark = LarkChannel(app_id=daemon.lark_app_id, app_secret=daemon.lark_app_secret)
    if telegram is None and daemon.telegram_bot_token:
        telegram = TelegramChannel(bot_token=daemon.telegram_bot_token)
    if slack is None and daemon.slack_bot_token:
        slack = SlackChannel(bot_token=daemon.slack_bot_token)
    if wecom is None and daemon.wecom_corp_id and daemon.wecom_corp_secret and daemon.wecom_agent_id:
        wecom = WeComChannel(
            corp_id=daemon.wecom_corp_id,
            corp_secret=daemon.wecom_corp_secret,
            agent_id=daemon.wecom_agent_id,
        )

    app = FastAPI(title="Anthill daemon", version="0.1.5")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "nation": nation.name,
            "citizens": len(nation.agents),
            "channels": {
                "lark": lark is not None,
                "telegram": telegram is not None,
                "slack": slack is not None,
                "wecom": wecom is not None,
            },
        }

    @app.post("/lark/webhook")
    async def lark_webhook(request: _Request) -> dict[str, Any]:
        payload = await request.json()
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}
        if lark is None:
            return {"error": "lark not configured"}
        token = payload.get("header", {}).get("token") or payload.get("token")
        if daemon.lark_verification_token and token != daemon.lark_verification_token:
            return {"error": "bad verification token"}
        msg = LarkChannel.parse_event(payload)
        if msg is None:
            return {"ignored": True}
        asyncio.create_task(handle_message(msg, nation, lark, config))
        return {"ok": True}

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: _Request) -> dict[str, Any]:
        if telegram is None:
            return {"error": "telegram not configured"}
        payload = await request.json()
        msg = TelegramChannel.parse_event(payload)
        if msg is None:
            return {"ignored": True}
        asyncio.create_task(handle_message(msg, nation, telegram, config))
        return {"ok": True}

    @app.post("/slack/webhook")
    async def slack_webhook(request: _Request) -> dict[str, Any]:
        payload = await request.json()
        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}
        if slack is None:
            return {"error": "slack not configured"}
        msg = SlackChannel.parse_event(payload)
        if msg is None:
            return {"ignored": True}
        asyncio.create_task(handle_message(msg, nation, slack, config))
        return {"ok": True}

    # Mount the MCP JSON-RPC endpoint at /mcp on the same app.
    try:
        from anthill.mcp.server import _handle as _mcp_handle
        from anthill.plugins import default_registry as _default_registry

        @app.post("/mcp")
        async def mcp_endpoint(request: _Request) -> dict[str, Any]:
            body = await request.json()
            rpc_id = body.get("id")
            method = body.get("method", "")
            params = body.get("params", {}) or {}
            try:
                result = await _mcp_handle(method, params, _default_registry)
            except Exception as e:  # noqa: BLE001
                return {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32603, "message": str(e)},
                }
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
    except Exception:  # noqa: BLE001
        pass

    @app.post("/wecom/webhook")
    async def wecom_webhook(request: _Request) -> dict[str, Any]:
        """WeCom payload — assumes the caller already decrypted the XML.

        WeCom's encrypted callbacks require an out-of-band decrypt step
        with the Token+EncodingAESKey. A proxy or wxcrypt helper feeds
        Anthill the plaintext dict; we focus on routing, not crypto.
        Set ANTHILL_WECOM_ACCEPT_PLAINTEXT=1 to acknowledge this design.
        """
        if wecom is None:
            return {"error": "wecom not configured"}
        payload = await request.json()
        msg = WeComChannel.parse_event(payload)
        if msg is None:
            return {"ignored": True}
        asyncio.create_task(handle_message(msg, nation, wecom, config))
        return {"ok": True}

    return app


def serve(daemon_config: DaemonConfig | None = None) -> None:
    """Run the daemon. Blocking. Logs to stderr."""
    try:
        import uvicorn
    except ImportError as e:
        raise RuntimeError(
            "Daemon mode requires the [daemon] extras. "
            "Install with: pip install 'anthill-agent[daemon]'"
        ) from e

    daemon = daemon_config or DaemonConfig.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log.info("starting Anthill daemon on %s:%s (nation=%s)",
             daemon.host, daemon.port, daemon.nation_name)
    app = build_app(daemon)
    uvicorn.run(app, host=daemon.host, port=daemon.port)
