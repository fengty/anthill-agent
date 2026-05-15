"""Provider tests with mocked HTTP — no API keys required."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from anthill.models.deepseek import DeepSeekProvider
from anthill.models.minimax import MiniMaxProvider
from anthill.models.registry import get_provider, known_providers


@pytest.mark.asyncio
async def test_deepseek_parses_response() -> None:
    provider = DeepSeekProvider(api_key="test-key")
    mock_response = AsyncMock()
    mock_response.json = lambda: {
        "choices": [{"message": {"content": "hello from deepseek"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await provider.complete("hi")

    assert result.text == "hello from deepseek"
    assert result.input_tokens == 5
    assert result.output_tokens == 3
    assert result.model == "deepseek-chat"


@pytest.mark.asyncio
async def test_minimax_parses_response() -> None:
    provider = MiniMaxProvider(api_key="test-key", group_id="grp-1")
    mock_response = AsyncMock()
    mock_response.json = lambda: {
        "choices": [{"message": {"content": "hello from minimax"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 4},
    }
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await provider.complete("hi")

    assert result.text == "hello from minimax"
    assert result.input_tokens == 7
    assert result.output_tokens == 4


def test_deepseek_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHILL_DEEPSEEK_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key"):
        DeepSeekProvider()


def test_minimax_requires_group_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHILL_MINIMAX_GROUP", raising=False)
    monkeypatch.delenv("MINIMAX_GROUP_ID", raising=False)
    with pytest.raises(RuntimeError, match="group_id"):
        MiniMaxProvider(api_key="test")


def test_registry_known_providers() -> None:
    names = known_providers()
    assert "deepseek" in names
    assert "minimax" in names


def test_registry_unknown_raises() -> None:
    with pytest.raises(KeyError, match="Unknown model"):
        get_provider("not-a-real-model")
