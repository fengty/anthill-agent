"""Expose Anthill plugins as an MCP server.

A small JSON-RPC over HTTP endpoint. Supports:

    initialize        protocol handshake
    tools/list        list registered plugins as MCP tools
    tools/call        invoke a plugin by name with arguments

Mount path: /mcp on the same FastAPI app the daemon already runs. The
daemon can host webhooks (Lark/Slack/etc) and MCP tools simultaneously
without conflict.
"""

from __future__ import annotations

from typing import Any

from anthill.plugins import default_registry
from anthill.plugins.base import PluginRegistry


PROTOCOL_VERSION = "2024-11-05"  # MCP version we target


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
            "serverInfo": {"name": "anthill", "version": "0.1.7"},
            "capabilities": {"tools": {"listChanged": False}},
        }
    if method == "tools/list":
        return {"tools": [_plugin_to_tool(p) for p in registry.list()]}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
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
