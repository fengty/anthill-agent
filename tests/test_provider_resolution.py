"""Tests for the new config-driven provider resolution.

Covers:
  - Resolving via UserConfig wins over the legacy alias map
  - Legacy alias map still works for backwards compat
  - Custom OpenAI-compatible endpoint resolved correctly
  - Anthropic provider gets the right adapter
  - Missing key raises a clear RuntimeError
  - Unknown name lists known options
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.userconfig import (
    ModelEntry,
    UserConfig,
    save_config,
    save_secrets,
)
from anthill.models import get_provider
from anthill.models.deepseek import DeepSeekProvider
from anthill.models.openai_compatible import OpenAICompatibleProvider


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))
    # Remove env vars so legacy aliases don't accidentally satisfy lookups.
    for var in (
        "ANTHILL_DEEPSEEK_KEY", "DEEPSEEK_API_KEY",
        "ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY",
        "ANTHILL_MINIMAX_GROUP", "MINIMAX_GROUP_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def test_user_config_wins_over_alias_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured 'deepseek' must use the configured key, not the env."""
    save_config(
        UserConfig(
            default_model="deepseek",
            models=[
                ModelEntry(
                    name="deepseek",
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref="model.deepseek",
                )
            ],
        )
    )
    save_secrets({"model.deepseek": "sk-from-config"})
    provider = get_provider("deepseek")
    assert isinstance(provider, DeepSeekProvider)
    assert provider.api_key == "sk-from-config"


def test_legacy_alias_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no UserConfig but a real env key, the alias path still works."""
    monkeypatch.setenv("ANTHILL_DEEPSEEK_KEY", "sk-from-env")
    provider = get_provider("deepseek-chat")
    assert isinstance(provider, DeepSeekProvider)
    assert provider.api_key == "sk-from-env"


def test_openai_compatible_resolves_for_openai() -> None:
    save_config(
        UserConfig(
            models=[
                ModelEntry(
                    name="gpt",
                    provider="openai",
                    model="gpt-4o-mini",
                    secret_ref="model.gpt",
                )
            ]
        )
    )
    save_secrets({"model.gpt": "sk-openai"})
    provider = get_provider("gpt")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.provider_name == "openai"


def test_anthropic_provider_resolved() -> None:
    save_config(
        UserConfig(
            models=[
                ModelEntry(
                    name="claude",
                    provider="anthropic",
                    model="claude-sonnet-4-5",
                    secret_ref="model.claude",
                )
            ]
        )
    )
    save_secrets({"model.claude": "sk-ant-xyz"})
    provider = get_provider("claude")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.provider_name == "anthropic"


def test_custom_endpoint_requires_base_url() -> None:
    save_config(
        UserConfig(
            models=[
                ModelEntry(
                    name="local",
                    provider="custom",
                    model="my-local-llm",
                    secret_ref="model.local",
                    base_url="http://127.0.0.1:11434/v1",
                )
            ]
        )
    )
    save_secrets({"model.local": "k"})
    provider = get_provider("local")
    assert provider.base_url == "http://127.0.0.1:11434/v1"


def test_missing_secret_raises_clearly() -> None:
    save_config(
        UserConfig(
            models=[
                ModelEntry(
                    name="orphan",
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref="model.orphan",
                )
            ]
        )
    )
    # No save_secrets call — the secret_ref is dangling.
    with pytest.raises(RuntimeError, match="no API key"):
        get_provider("orphan")


def test_unknown_name_lists_known_options() -> None:
    save_config(
        UserConfig(
            models=[
                ModelEntry(
                    name="mine",
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref="r",
                )
            ]
        )
    )
    save_secrets({"r": "k"})
    with pytest.raises(KeyError) as exc:
        get_provider("ghost")
    assert "mine" in str(exc.value)        # user-configured name listed
    assert "deepseek-chat" in str(exc.value)  # legacy alias also listed
