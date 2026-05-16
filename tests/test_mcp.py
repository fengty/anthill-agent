"""Tests for the MCP server handler and remote client adapter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from anthill.mcp.client import McpClient, McpRemotePlugin
from anthill.mcp.server import _handle
from anthill.plugins.base import Plugin, PluginRegistry, PluginResult


class _Echo(Plugin):
    name = "echo"
    description = "Echo its input back"

    async def call(self, **kwargs):
        return PluginResult(output=str(kwargs))


def _reg() -> PluginRegistry:
    reg = PluginRegistry()
    reg.register(_Echo())
    return reg


def test_initialize_returns_server_info() -> None:
    result = asyncio.run(_handle("initialize", {}, _reg()))
    assert "protocolVersion" in result
    assert result["serverInfo"]["name"] == "anthill"


def test_tools_list_exposes_plugins() -> None:
    result = asyncio.run(_handle("tools/list", {}, _reg()))
    names = [t["name"] for t in result["tools"]]
    assert "echo" in names
    assert any("description" in t for t in result["tools"])


def test_tools_call_invokes_plugin() -> None:
    params = {"name": "echo", "arguments": {"x": 1, "y": "z"}}
    result = asyncio.run(_handle("tools/call", params, _reg()))
    assert not result["isError"]
    text = result["content"][0]["text"]
    assert "x" in text and "1" in text


def test_tools_call_unknown_tool_marks_error() -> None:
    params = {"name": "no-such-tool", "arguments": {}}
    result = asyncio.run(_handle("tools/call", params, _reg()))
    assert result["isError"]


def test_handle_rejects_unknown_method() -> None:
    with pytest.raises(ValueError):
        asyncio.run(_handle("definitely/not/a/method", {}, _reg()))


# Client-side tests with mocked HTTP


@pytest.mark.asyncio
async def test_client_lists_tools_from_remote() -> None:
    mock_response = AsyncMock()
    mock_response.json = lambda: {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [{"name": "search", "description": "Search Notion"}]},
    }
    mock_response.raise_for_status = lambda: None
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        client = McpClient(url="https://mcp.example.com")
        tools = await client.list_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "search"


@pytest.mark.asyncio
async def test_client_register_with_prefix() -> None:
    mock_response = AsyncMock()
    mock_response.json = lambda: {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"tools": [
            {"name": "search", "description": "x"},
            {"name": "create_page", "description": "y"},
        ]},
    }
    mock_response.raise_for_status = lambda: None
    reg = PluginRegistry()
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        client = McpClient(url="https://mcp.example.com")
        count = await client.register_with(reg, prefix="notion")
    assert count == 2
    assert "notion.search" in reg.names()
    assert "notion.create_page" in reg.names()


@pytest.mark.asyncio
async def test_remote_plugin_forwards_call() -> None:
    """A registered McpRemotePlugin should call the remote and return text."""
    mock_response_list = AsyncMock()
    mock_response_list.json = lambda: {
        "jsonrpc": "2.0", "id": 1,
        "result": {"tools": [{"name": "ping", "description": "say pong"}]},
    }
    mock_response_list.raise_for_status = lambda: None

    mock_response_call = AsyncMock()
    mock_response_call.json = lambda: {
        "jsonrpc": "2.0", "id": 2,
        "result": {
            "content": [{"type": "text", "text": "pong"}],
            "isError": False,
        },
    }
    mock_response_call.raise_for_status = lambda: None

    reg = PluginRegistry()
    with patch("httpx.AsyncClient.post", side_effect=[mock_response_list, mock_response_call]):
        client = McpClient(url="https://mcp.example.com")
        await client.register_with(reg)
        remote = reg.get("ping")
        assert isinstance(remote, McpRemotePlugin)
        result = await remote.call(message="hello")
    assert result.ok
    assert result.output == "pong"
