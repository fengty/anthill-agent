"""Plugin tests with mocked HTTP — no live web calls."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from anthill.plugins import default_registry
from anthill.plugins.base import Plugin, PluginRegistry, PluginResult
from anthill.plugins.web import WebFetchPlugin, WebSearchPlugin, _strip_html


def test_registry_lists_builtin_plugins() -> None:
    names = default_registry.names()
    assert "web_fetch" in names
    assert "web_search" in names


def test_registry_describe_includes_descriptions() -> None:
    desc = default_registry.describe()
    assert "Fetch a URL" in desc
    assert "Search the web" in desc


def test_strip_html_collapses_whitespace() -> None:
    assert _strip_html("<p>Hello  <b>World</b></p>") == "Hello World"


def test_strip_html_handles_nested_tags() -> None:
    out = _strip_html("<div><a href='x'>link</a> text</div>")
    assert out == "link text"


class _CustomPlugin(Plugin):
    name = "custom"
    description = "custom test plugin"

    async def call(self, **kwargs):
        return PluginResult(output=kwargs)


def test_custom_plugin_registration() -> None:
    reg = PluginRegistry()
    reg.register(_CustomPlugin())
    assert "custom" in reg.names()
    plugin = reg.get("custom")
    assert plugin is not None
    result = asyncio.run(plugin.call(x=1, y=2))
    assert result.ok
    assert result.output == {"x": 1, "y": 2}


def test_register_rejects_anonymous_plugin() -> None:
    reg = PluginRegistry()
    anon = _CustomPlugin()
    anon.name = ""
    with pytest.raises(ValueError):
        reg.register(anon)


@pytest.mark.asyncio
async def test_web_fetch_invalid_url() -> None:
    result = await WebFetchPlugin().call(url="not-a-url")
    assert not result.ok
    assert "invalid url" in result.error


@pytest.mark.asyncio
async def test_web_fetch_returns_stripped_text() -> None:
    mock_response = AsyncMock()
    mock_response.text = "<html><body><p>Hello World</p></body></html>"
    mock_response.url = "https://example.com"
    mock_response.status_code = 200
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        result = await WebFetchPlugin().call(url="https://example.com")

    assert result.ok
    assert "Hello World" in result.output


@pytest.mark.asyncio
async def test_web_search_empty_query() -> None:
    result = await WebSearchPlugin().call(query="")
    assert not result.ok
    assert "empty query" in result.error


@pytest.mark.asyncio
async def test_web_search_no_key_lists_all_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "BOCHA_API_KEY", "ANTHILL_BOCHA_KEY",
        "TAVILY_API_KEY", "ANTHILL_TAVILY_KEY",
        "SERPER_API_KEY", "ANTHILL_SERPER_KEY",
        "BRAVE_API_KEY", "ANTHILL_BRAVE_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    result = await WebSearchPlugin().call(query="x")
    assert not result.ok
    # All four providers should be mentioned so the user knows the choices.
    for var in ("BOCHA_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY", "BRAVE_API_KEY"):
        assert var in result.error


@pytest.mark.asyncio
async def test_web_search_bocha_parses_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOCHA_API_KEY", "test-key")
    mock_response = AsyncMock()
    mock_response.json = lambda: {
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "Anthill Agent",
                        "url": "https://github.com/fengty/anthill-agent",
                        "snippet": "Pheromone-routed multi-agent framework.",
                    }
                ]
            }
        }
    }
    mock_response.raise_for_status = lambda: None
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await WebSearchPlugin().call(query="anthill", top_k=3)
    assert result.ok
    assert result.metadata["engine"] == "bocha"
    assert result.output[0]["title"] == "Anthill Agent"
    assert "github.com" in result.output[0]["url"]
