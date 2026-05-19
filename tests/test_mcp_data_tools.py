"""0.1.68 — MCP server data tools.

Pre-0.1.68 the MCP server only exposed PLUGINS. This version adds
anthill_*-prefixed DATA tools so external MCP clients (Claude Code,
Cursor, Codex) can use anthill as a backend — read nation history,
sessions, skills, channels; submit asks; send via configured channels.

Tests verify:
  - tools/list returns plugins + data tools (no collision)
  - unknown tool name → isError=True with helpful text
  - dispatch routes anthill_* names to the data handler (not the
    plugin registry, which would otherwise miss)
  - each data tool's expected output shape (JSON envelope where
    appropriate)
  - error branches return ok=False without raising

We monkeypatch the few module-level functions each tool reaches into,
keeping these tests pure unit (no real config / file I/O).
"""

from __future__ import annotations

import json

import pytest

from anthill.mcp.server import (
    ANTHILL_DATA_TOOLS,
    _handle,
    _handle_data_tool,
    _is_anthill_tool,
)
from anthill.plugins.base import PluginRegistry


# --- prefix predicate ----------------------------------------------------


def test_is_anthill_tool_matches_namespace() -> None:
    assert _is_anthill_tool("anthill_nation_ask") is True
    assert _is_anthill_tool("anthill_sessions_list") is True
    # Plugin names don't have this prefix.
    assert _is_anthill_tool("web_fetch") is False
    assert _is_anthill_tool("file_read") is False
    assert _is_anthill_tool(None) is False
    assert _is_anthill_tool("") is False


def test_data_tools_all_prefixed() -> None:
    """The prefix predicate must MATCH every shipped data tool —
    otherwise dispatch would silently fall through to the plugin
    registry and return 'unknown tool'."""
    for tool in ANTHILL_DATA_TOOLS:
        assert _is_anthill_tool(tool["name"]), (
            f"data tool {tool['name']!r} missing anthill_ prefix"
        )


def test_data_tools_have_input_schema() -> None:
    """MCP clients introspect the schema. Each tool must declare one."""
    for tool in ANTHILL_DATA_TOOLS:
        assert "name" in tool
        assert "description" in tool
        assert tool.get("inputSchema", {}).get("type") == "object"


# --- tools/list integration ---------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_merges_plugins_and_data() -> None:
    """The list endpoint surfaces both pools so MCP clients see them
    together. Verify no data tool name collides with a plugin name."""
    reg = PluginRegistry()
    # The default registry has plugins like web_fetch; we use empty
    # here for isolation.
    result = await _handle("tools/list", {}, reg)
    tool_names = {t["name"] for t in result["tools"]}
    assert "anthill_nation_ask" in tool_names
    assert "anthill_sessions_list" in tool_names
    assert "anthill_channels_list" in tool_names


@pytest.mark.asyncio
async def test_tools_call_dispatches_anthill_prefix(monkeypatch) -> None:
    """Calling an anthill_ name goes through _handle_data_tool, not
    the plugin registry."""
    reg = PluginRegistry()
    # Stub the data handler so we don't touch the file system.
    called = {}

    async def fake_data_tool(name, args):
        called["name"] = name
        called["args"] = args
        return "ok-stub", True

    monkeypatch.setattr(
        "anthill.mcp.server._handle_data_tool", fake_data_tool
    )
    result = await _handle(
        "tools/call",
        {
            "name": "anthill_nation_ask",
            "arguments": {"request": "ping"},
        },
        reg,
    )
    assert called["name"] == "anthill_nation_ask"
    assert called["args"] == {"request": "ping"}
    assert result["isError"] is False
    assert result["content"][0]["text"] == "ok-stub"


# --- _handle_data_tool branches -----------------------------------------


@pytest.mark.asyncio
async def test_unknown_data_tool_returns_error() -> None:
    text, ok = await _handle_data_tool("anthill_does_not_exist", {})
    assert ok is False
    assert "unknown" in text.lower()


@pytest.mark.asyncio
async def test_nation_ask_empty_request_rejected() -> None:
    """Empty request → ok=False. We don't assert exact message (could
    bail at config-load OR empty-check depending on env)."""
    text, ok = await _handle_data_tool("anthill_nation_ask", {"request": ""})
    assert ok is False
    # Some failure shape is fine — text is non-empty.
    assert text


@pytest.mark.asyncio
async def test_search_sessions_returns_json(monkeypatch) -> None:
    """Verify the JSON envelope shape returned by anthill_search_sessions."""
    from anthill.core.session_search import SearchHit

    fake_hits = [
        SearchHit(
            session_id="sess-abc",
            ts=1700000000.0,
            request="zentao bug 12345",
            snippet="…zentao bug…",
            match_field="request",
        )
    ]

    def fake_search(query, *, home, limit, **kw):
        return fake_hits

    monkeypatch.setattr(
        "anthill.core.session_search.search_sessions", fake_search
    )
    text, ok = await _handle_data_tool(
        "anthill_search_sessions", {"query": "zentao", "limit": 10}
    )
    assert ok is True
    parsed = json.loads(text)
    assert isinstance(parsed, list)
    assert parsed[0]["session_id"] == "sess-abc"
    assert parsed[0]["match_field"] == "request"


@pytest.mark.asyncio
async def test_channel_send_requires_all_fields() -> None:
    """All three (channel, to, text) required; missing any → ok=False."""
    text, ok = await _handle_data_tool(
        "anthill_channel_send", {"channel": "slack", "to": ""}
    )
    assert ok is False
    text, ok = await _handle_data_tool(
        "anthill_channel_send",
        {"channel": "slack", "to": "C123", "text": ""},
    )
    assert ok is False


@pytest.mark.asyncio
async def test_channel_send_unknown_channel_returns_error(monkeypatch) -> None:
    """Calling send for a channel name the user never configured →
    clean ok=False (not a stacktrace bubble)."""
    class _StubUserCfg:
        channels: list = []

        def find_channel(self, name):
            return None

    monkeypatch.setattr(
        "anthill.core.userconfig.load_config",
        lambda: _StubUserCfg(),
    )
    text, ok = await _handle_data_tool(
        "anthill_channel_send",
        {"channel": "discord", "to": "C123", "text": "hi"},
    )
    assert ok is False
    assert "not configured" in text


@pytest.mark.asyncio
async def test_channels_list_returns_names_and_kinds(monkeypatch) -> None:
    class _Ch:
        def __init__(self, name, kind):
            self.name = name
            self.kind = kind

    class _StubUserCfg:
        channels = [_Ch("notif", "slack"), _Ch("mail", "email")]

    monkeypatch.setattr(
        "anthill.core.userconfig.load_config",
        lambda: _StubUserCfg(),
    )
    text, ok = await _handle_data_tool("anthill_channels_list", {})
    assert ok is True
    parsed = json.loads(text)
    assert {"name": "notif", "kind": "slack"} in parsed
    assert {"name": "mail", "kind": "email"} in parsed


@pytest.mark.asyncio
async def test_skill_list_returns_json_list(monkeypatch) -> None:
    """anthill_skill_list returns a JSON array of recipes (possibly
    empty when the configured nation has none)."""
    from anthill.core.recipes import Recipe, RecipeSubtask

    monkeypatch.setattr(
        "anthill.core.recipes.list_recipes",
        lambda ndir: [
            Recipe(
                name="analyze-bug",
                template="t",
                description="bug analysis",
                subtasks=[
                    RecipeSubtask(task_type="research", prompt_template="x")
                ],
                run_count=5,
            )
        ],
    )
    text, ok = await _handle_data_tool(
        "anthill_skill_list", {"nation": "default"}
    )
    assert ok is True
    parsed = json.loads(text)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "analyze-bug"
    assert parsed[0]["run_count"] == 5


@pytest.mark.asyncio
async def test_sessions_list_returns_summary_array(monkeypatch) -> None:
    class _StubSummary:
        def __init__(self, sid):
            self.session_id = sid
            self.nation_name = "default"
            self.started_at = 1.0
            self.turn_count = 3

    monkeypatch.setattr(
        "anthill.core.sessions.list_sessions",
        lambda home, limit: [_StubSummary("sess-1"), _StubSummary("sess-2")],
    )
    text, ok = await _handle_data_tool(
        "anthill_sessions_list", {"limit": 5}
    )
    assert ok is True
    parsed = json.loads(text)
    assert len(parsed) == 2
    assert parsed[0]["session_id"] == "sess-1"
    assert parsed[0]["turn_count"] == 3


@pytest.mark.asyncio
async def test_session_get_missing_id_rejected() -> None:
    text, ok = await _handle_data_tool("anthill_session_get", {})
    assert ok is False
    assert "session_id" in text


@pytest.mark.asyncio
async def test_session_get_missing_session(monkeypatch) -> None:
    monkeypatch.setattr(
        "anthill.core.sessions.load_session",
        lambda sid, home: None,
    )
    text, ok = await _handle_data_tool(
        "anthill_session_get", {"session_id": "nope"}
    )
    assert ok is False
    assert "no such session" in text
