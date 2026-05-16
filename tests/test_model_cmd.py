"""Tests for `anthill model` subcommands.

Uses Click's CliRunner to invoke commands as a user would. Real HTTP
isn't exercised here — `model test` is tested at the helper level with
mocked httpx, and the CLI just relays.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from anthill.cli.model_cmd import model as model_group
from anthill.core.userconfig import (
    ModelEntry,
    UserConfig,
    load_config,
    load_secrets,
    save_config,
    save_secrets,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


def _seed(name: str = "deepseek") -> None:
    save_config(
        UserConfig(
            default_model=name,
            models=[
                ModelEntry(
                    name=name,
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref=f"model.{name}",
                )
            ],
        )
    )
    save_secrets({f"model.{name}": "sk-fake"})


def test_list_empty_shows_hint() -> None:
    runner = CliRunner()
    result = runner.invoke(model_group, ["list"])
    assert result.exit_code == 0
    assert "No models configured" in result.output


def test_list_shows_seeded_model() -> None:
    _seed()
    result = CliRunner().invoke(model_group, ["list"])
    assert result.exit_code == 0
    assert "deepseek" in result.output
    assert "★" in result.output  # default marker


def test_add_with_full_flags() -> None:
    result = CliRunner().invoke(
        model_group,
        [
            "add", "work",
            "--provider", "deepseek",
            "--model", "deepseek-chat",
            "--key", "sk-real",
        ],
    )
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert cfg.find_model("work") is not None
    assert load_secrets()["model.work"] == "sk-real"


def test_add_rejects_unknown_provider() -> None:
    result = CliRunner().invoke(
        model_group,
        [
            "add", "x",
            "--provider", "made-up",
            "--model", "x",
            "--key", "k",
        ],
    )
    assert result.exit_code != 0
    assert "Unknown provider" in result.output


def test_add_custom_requires_base_url() -> None:
    result = CliRunner().invoke(
        model_group,
        [
            "add", "x",
            "--provider", "custom",
            "--model", "my-llm",
            "--key", "k",
        ],
    )
    assert result.exit_code != 0
    assert "requires --base-url" in result.output


def test_use_switches_default() -> None:
    _seed("a")
    save_secrets({"model.a": "k", "model.b": "k"})
    cfg = load_config()
    cfg.models.append(
        ModelEntry(name="b", provider="deepseek", model="deepseek-chat", secret_ref="model.b")
    )
    save_config(cfg)
    result = CliRunner().invoke(model_group, ["use", "b"])
    assert result.exit_code == 0
    assert load_config().default_model == "b"


def test_remove_with_yes_drops_model_and_secret() -> None:
    _seed("a")
    result = CliRunner().invoke(model_group, ["remove", "a", "--yes"])
    assert result.exit_code == 0
    assert load_config().find_model("a") is None
    assert "model.a" not in load_secrets()


def test_rename_moves_secret_too() -> None:
    _seed("old")
    result = CliRunner().invoke(model_group, ["rename", "old", "new"])
    assert result.exit_code == 0
    assert load_config().find_model("new") is not None
    secrets = load_secrets()
    assert "model.new" in secrets
    assert "model.old" not in secrets


def test_show_displays_masked_key() -> None:
    _seed("work")
    save_secrets({"model.work": "sk-12345678abcdef"})
    result = CliRunner().invoke(model_group, ["show", "work"])
    assert result.exit_code == 0
    assert "sk-1" in result.output       # prefix shown
    assert "sk-12345678abcdef" not in result.output  # full key never shown


def test_test_command_calls_provider_endpoint() -> None:
    _seed("work")
    mock_response = AsyncMock()
    mock_response.json = lambda: {"usage": {"completion_tokens": 4}}
    mock_response.raise_for_status = lambda: None
    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = CliRunner().invoke(model_group, ["test", "work"])
    assert result.exit_code == 0
    assert "✓ ok" in result.output
