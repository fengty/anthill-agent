"""Consume a remote MCP server as an Anthill plugin source.

`McpRemotePlugin` is a thin adapter that wraps a single remote tool
and presents it as an Anthill Plugin. `McpClient.register_with(registry)`
discovers all tools the server advertises and registers them.

Transport: HTTP JSON-RPC against an MCP endpoint URL. We support the
single-shot request/response shape (no SSE streaming for v0.1.x).

Real-world use:
    client = McpClient(url="https://mcp.notion.com/...")
    await client.register_with(default_registry)
    # → Notion tools now appear in `anthill plugins list`
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx

from anthill.plugins.base import Plugin, PluginRegistry, PluginResult


class McpClient:
    """Talk JSON-RPC to a single MCP endpoint."""

    def __init__(self, *, url: str, name: str = "remote", headers: dict | None = None) -> None:
        self.url = url
        self.name = name
        self.headers = headers or {}
        self._id_counter = itertools.count(1)

    async def _call(self, method: str, params: dict | None = None) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._id_counter),
            "method": method,
            "params": params or {},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(self.url, json=payload, headers=self.headers)
            response.raise_for_status()
            data = response.json()
        if "error" in data:
            raise RuntimeError(f"MCP error from {self.url}: {data['error']}")
        return data.get("result") or {}

    async def list_tools(self) -> list[dict]:
        result = await self._call("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> PluginResult:
        result = await self._call("tools/call", {"name": name, "arguments": arguments})
        is_error = bool(result.get("isError"))
        # MCP returns content as a list of typed blocks; we collapse to text.
        content_blocks = result.get("content") or []
        text = "\n".join(
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        )
        return PluginResult(
            output=text if not is_error else None,
            ok=not is_error,
            error=text if is_error else None,
            metadata={"source": self.name, "url": self.url},
        )

    async def register_with(self, registry: PluginRegistry, *, prefix: str | None = None) -> int:
        """Discover tools and register each as an Anthill Plugin.

        Returns the count registered. Prefix is added to tool names so a
        Notion `search` and a GitHub `search` can coexist.
        """
        tools = await self.list_tools()
        registered = 0
        for tool in tools:
            tool_name = tool.get("name")
            if not tool_name:
                continue
            full_name = f"{prefix}.{tool_name}" if prefix else tool_name
            registry.register(
                McpRemotePlugin(
                    client=self,
                    remote_name=tool_name,
                    local_name=full_name,
                    description=tool.get("description", "remote MCP tool"),
                )
            )
            registered += 1
        return registered


class McpRemotePlugin(Plugin):
    """Plugin that forwards a call to a remote MCP server."""

    def __init__(
        self,
        *,
        client: McpClient,
        remote_name: str,
        local_name: str,
        description: str,
    ) -> None:
        self._client = client
        self._remote_name = remote_name
        self.name = local_name
        self.description = description

    async def call(self, **kwargs: Any) -> PluginResult:
        return await self._client.call_tool(self._remote_name, kwargs)
