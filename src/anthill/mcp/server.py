"""Expose Anthill plugins AND nation data as an MCP server.

A small JSON-RPC over HTTP endpoint. Supports:

    initialize        protocol handshake
    tools/list        list registered plugins + anthill-data tools
    tools/call        invoke a plugin OR a data tool by name

Mount path: /mcp on the same FastAPI app the daemon already runs. The
daemon can host webhooks (Lark/Slack/etc) and MCP tools simultaneously
without conflict.

0.1.68 — added "data tools" alongside plugins. Mirrors Hermes's pattern
of exposing the agent's conversation history / sessions / channels so
other agents (Claude Code, Cursor, Codex) can use anthill as a
backend rather than re-implementing all of it. Tools shipped:

  anthill_nation_ask         submit an ask, return final output
  anthill_history            recent asks for a nation
  anthill_sessions_list      session ids + summaries
  anthill_session_get        all turns in one session
  anthill_search_sessions    cross-session grep (uses 0.1.63)
  anthill_skill_list         saved skills with usage stats
  anthill_channels_list      configured channels
  anthill_channel_send       send via a configured channel

Each data tool is namespaced `anthill_*` so it can't collide with a
user-registered plugin's name.
"""

from __future__ import annotations

from typing import Any

from anthill.plugins import default_registry
from anthill.plugins.base import PluginRegistry


PROTOCOL_VERSION = "2024-11-05"  # MCP version we target


# 0.1.68 — anthill-data tool descriptors. Each has a name (prefixed
# anthill_ to avoid plugin collisions), a description, and a JSON
# Schema input contract. Dispatch lives in _handle_data_tool below.
ANTHILL_DATA_TOOLS: list[dict] = [
    {
        "name": "anthill_nation_ask",
        "description": (
            "Submit an ask to an anthill nation; returns the final synthesized output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "nation": {"type": "string", "default": "default"},
            },
            "required": ["request"],
        },
    },
    {
        "name": "anthill_history",
        "description": "Recent ask history for a nation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "nation": {"type": "string", "default": "default"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "anthill_sessions_list",
        "description": "List session ids with first/last timestamps.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "anthill_session_get",
        "description": "Read all turns from one session.",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "anthill_search_sessions",
        "description": "Grep across all session JSONL. Supports /regex/ syntax.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "anthill_skill_list",
        "description": "List saved skills for a nation with usage stats.",
        "inputSchema": {
            "type": "object",
            "properties": {"nation": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "anthill_channels_list",
        "description": "List configured IM channels (lark/slack/telegram/discord/email/wecom).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "anthill_channel_send",
        "description": "Send a text message via a configured channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "to": {"type": "string"},
                "text": {"type": "string"},
                "reply_to": {"type": "string"},
                "thread_id": {"type": "string"},
            },
            "required": ["channel", "to", "text"],
        },
    },
]


def _is_anthill_tool(name: str | None) -> bool:
    """True when `name` is an anthill_*-prefixed data tool."""
    return bool(name) and name.startswith("anthill_")


def _plugin_to_tool(plugin) -> dict:  # noqa: ANN001
    """Convert a Plugin to an MCP tool descriptor.

    MCP's tool schema is JSON Schema for inputSchema. We declare it as
    a permissive object since our plugins take **kwargs; the descriptions
    in the plugin name carry the contract.
    """
    return {
        "name": plugin.name,
        "description": plugin.description,
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
    }


async def _handle(method: str, params: dict, registry: PluginRegistry) -> dict:
    """Dispatch one JSON-RPC method to the appropriate handler."""
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "anthill", "version": "0.1.68"},
            "capabilities": {"tools": {"listChanged": False}},
        }
    if method == "tools/list":
        plugin_tools = [_plugin_to_tool(p) for p in registry.list()]
        # 0.1.68 — concat anthill-data tools so external MCP clients
        # see them alongside plugins. Different namespaces don't
        # collide thanks to the anthill_ prefix.
        return {"tools": plugin_tools + ANTHILL_DATA_TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        # 0.1.68 — dispatch anthill_*-prefixed names to the data
        # handler instead of the plugin registry.
        if _is_anthill_tool(name):
            text, ok = await _handle_data_tool(name, args)
            return {
                "content": [{"type": "text", "text": text}],
                "isError": not ok,
            }
        plugin = registry.get(name) if name else None
        if plugin is None:
            return {
                "content": [{"type": "text", "text": f"unknown tool: {name!r}"}],
                "isError": True,
            }
        # v0.7.2 — when the caller passes a nation_dir via the special
        # `_anthill_nation_dir` argument key, route the call through
        # `record_plugin_call` so usage telemetry lands on disk. Without
        # the key we fall back to the plain call() path (used by tests
        # and by callers that don't track usage yet).
        nation_dir = args.pop("_anthill_nation_dir", None)
        if nation_dir is not None:
            from pathlib import Path as _Path
            from anthill.core.plugin_usage import record_plugin_call
            result = await record_plugin_call(plugin, _Path(nation_dir), **args)
        else:
            result = await plugin.call(**args)
        text = str(result.output) if result.output is not None else (result.error or "")
        return {
            "content": [{"type": "text", "text": text}],
            "isError": not result.ok,
        }
    raise ValueError(f"unknown method: {method!r}")


async def _handle_data_tool(name: str, args: dict) -> tuple[str, bool]:
    """Dispatch an anthill_*-prefixed data tool. Returns (text, ok).

    Each branch is small + self-contained. We never raise — any
    backend error becomes ok=False with the error text in the result,
    so the MCP client sees a clean isError signal.
    """
    import json as _json

    try:
        from anthill.config import AnthillConfig
        config = AnthillConfig.load()
    except Exception as e:  # noqa: BLE001
        return f"config load failed: {e}", False

    if name == "anthill_nation_ask":
        try:
            from anthill.channels.daemon import _load_or_create_nation
            from anthill.core.persistence import nation_dir
            req = args.get("request") or ""
            nation_name = args.get("nation") or "default"
            if not req.strip():
                return "request is empty", False
            nation = _load_or_create_nation(config, nation_name)
            result = await nation.ask(
                req, nation_dir=nation_dir(config.home, nation_name)
            )
            return result.final_output or "(no output)", True
        except Exception as e:  # noqa: BLE001
            return f"ask failed: {e}", False

    if name == "anthill_history":
        try:
            from anthill.core.history import load_history
            from anthill.core.persistence import nation_dir
            nation_name = args.get("nation") or "default"
            limit = int(args.get("limit") or 10)
            entries = load_history(
                nation_dir(config.home, nation_name), limit=limit
            )
            return _json.dumps(
                [
                    {
                        "id": e.id,
                        "ts": e.timestamp,
                        "request": e.request,
                        "outcomes": e.outcomes,
                    }
                    for e in entries
                ],
                ensure_ascii=False,
            ), True
        except Exception as e:  # noqa: BLE001
            return f"history failed: {e}", False

    if name == "anthill_sessions_list":
        try:
            from anthill.core.sessions import list_sessions
            limit = int(args.get("limit") or 20)
            summaries = list_sessions(config.home, limit=limit)
            return _json.dumps(
                [
                    {
                        "session_id": s.session_id,
                        "nation": s.nation_name,
                        "started_at": s.started_at,
                        "turn_count": s.turn_count,
                    }
                    for s in summaries
                ],
                ensure_ascii=False,
            ), True
        except Exception as e:  # noqa: BLE001
            return f"sessions list failed: {e}", False

    if name == "anthill_session_get":
        try:
            from anthill.core.sessions import load_session
            sid = args.get("session_id") or ""
            if not sid:
                return "session_id required", False
            sess = load_session(sid, config.home)
            if sess is None:
                return f"no such session: {sid}", False
            return _json.dumps(
                [
                    {
                        "ts": t.ts,
                        "request": t.request,
                        "output": t.final_output,
                        "plan": t.plan,
                    }
                    for t in sess.turns
                ],
                ensure_ascii=False,
            ), True
        except Exception as e:  # noqa: BLE001
            return f"session get failed: {e}", False

    if name == "anthill_search_sessions":
        try:
            from anthill.core.session_search import search_sessions
            query = args.get("query") or ""
            limit = int(args.get("limit") or 20)
            hits = search_sessions(query, home=config.home, limit=limit)
            return _json.dumps(
                [
                    {
                        "session_id": h.session_id,
                        "ts": h.ts,
                        "snippet": h.snippet,
                        "match_field": h.match_field,
                    }
                    for h in hits
                ],
                ensure_ascii=False,
            ), True
        except Exception as e:  # noqa: BLE001
            return f"search failed: {e}", False

    if name == "anthill_skill_list":
        try:
            from anthill.core.persistence import nation_dir
            from anthill.core.recipes import list_recipes
            from anthill.core.skill_stats import format_skill_stats
            nation_name = args.get("nation") or "default"
            ndir = nation_dir(config.home, nation_name)
            recipes = list_recipes(ndir)
            return _json.dumps(
                [
                    {
                        "name": r.name,
                        "description": r.description,
                        "subtask_count": len(r.subtasks),
                        "run_count": r.run_count,
                        "stats": format_skill_stats(r),
                    }
                    for r in recipes
                ],
                ensure_ascii=False,
            ), True
        except Exception as e:  # noqa: BLE001
            return f"skill list failed: {e}", False

    if name == "anthill_channels_list":
        try:
            from anthill.core.userconfig import load_config as _load_user_cfg
            user_cfg = _load_user_cfg()
            return _json.dumps(
                [
                    {"name": c.name, "kind": c.kind}
                    for c in user_cfg.channels
                ],
                ensure_ascii=False,
            ), True
        except Exception as e:  # noqa: BLE001
            return f"channels list failed: {e}", False

    if name == "anthill_channel_send":
        try:
            from anthill.cli.channel_cmd import build_channel
            from anthill.core.userconfig import load_config as _load_user_cfg
            ch_name = args.get("channel") or ""
            to = args.get("to") or ""
            text = args.get("text") or ""
            if not (ch_name and to and text):
                return "channel, to, and text are all required", False
            user_cfg = _load_user_cfg()
            entry = user_cfg.find_channel(ch_name)
            if entry is None:
                return f"channel {ch_name!r} not configured", False
            built = build_channel(entry)
            if built is None:
                return f"channel {ch_name!r} not buildable (missing secrets?)", False
            await built.send(
                to=to,
                text=text,
                reply_to=args.get("reply_to"),
                thread_id=args.get("thread_id"),
            )
            return "ok", True
        except Exception as e:  # noqa: BLE001
            return f"send failed: {e}", False

    return f"unknown anthill data tool: {name!r}", False


def build_mcp_app(registry: PluginRegistry | None = None):
    """Return a FastAPI app exposing a JSON-RPC /mcp endpoint.

    Importing FastAPI here mirrors the daemon module's lazy import so
    the [daemon] extras stay optional.
    """
    try:
        from fastapi import FastAPI, Request
    except ImportError as e:
        raise RuntimeError(
            "MCP server needs the [daemon] extras. "
            "Install with: pip install 'anthill-agent[daemon]'"
        ) from e

    reg = registry or default_registry
    app = FastAPI(title="Anthill MCP", version="0.1.7")

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> dict[str, Any]:
        body = await request.json()
        rpc_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params", {}) or {}
        try:
            result = await _handle(method, params, reg)
        except Exception as e:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32603, "message": str(e)},
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    return app
