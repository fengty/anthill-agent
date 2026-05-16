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

from anthill.channels.base import Channel, ChannelMessage
from anthill.channels.lark import LarkChannel
from anthill.channels.slack import SlackChannel
from anthill.channels.telegram import TelegramChannel
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

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        return cls(
            nation_name=os.getenv("ANTHILL_DAEMON_NATION", "default"),
            host=os.getenv("ANTHILL_DAEMON_HOST", "0.0.0.0"),
            port=int(os.getenv("ANTHILL_DAEMON_PORT", "8765")),
            lark_app_id=os.getenv("ANTHILL_LARK_APP_ID"),
            lark_app_secret=os.getenv("ANTHILL_LARK_APP_SECRET"),
            lark_verification_token=os.getenv("ANTHILL_LARK_VERIFICATION_TOKEN"),
            telegram_bot_token=os.getenv("ANTHILL_TELEGRAM_BOT_TOKEN"),
            slack_bot_token=os.getenv("ANTHILL_SLACK_BOT_TOKEN"),
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
    try:
        from fastapi import FastAPI, Request
    except ImportError as e:
        raise RuntimeError(
            "Daemon mode requires the [daemon] extras. "
            "Install with: pip install 'anthill-agent[daemon]'"
        ) from e

    daemon = daemon_config or DaemonConfig.from_env()
    config = AnthillConfig.load()
    config.ensure_home()
    nation = _load_or_create_nation(config, daemon.nation_name)

    # Build channels for whichever credentials are present.
    lark: LarkChannel | None = None
    if daemon.lark_app_id and daemon.lark_app_secret:
        lark = LarkChannel(app_id=daemon.lark_app_id, app_secret=daemon.lark_app_secret)

    telegram: TelegramChannel | None = None
    if daemon.telegram_bot_token:
        telegram = TelegramChannel(bot_token=daemon.telegram_bot_token)

    slack: SlackChannel | None = None
    if daemon.slack_bot_token:
        slack = SlackChannel(bot_token=daemon.slack_bot_token)

    app = FastAPI(title="Anthill daemon", version="0.0.29")

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
            },
        }

    @app.post("/lark/webhook")
    async def lark_webhook(request: Request) -> dict[str, Any]:
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
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        if telegram is None:
            return {"error": "telegram not configured"}
        payload = await request.json()
        msg = TelegramChannel.parse_event(payload)
        if msg is None:
            return {"ignored": True}
        asyncio.create_task(handle_message(msg, nation, telegram, config))
        return {"ok": True}

    @app.post("/slack/webhook")
    async def slack_webhook(request: Request) -> dict[str, Any]:
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
