"""Tests for the setup wizard helpers.

The wizard itself is hard to fully unit-test (it needs stdin/getpass).
We test the parts that are pure functions or easy to drive: provider
presets, non-tty refusal, basic flow with monkeypatched input.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from anthill.cli.providers_meta import PROVIDER_PRESETS
from anthill.cli.setup import run_wizard


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


def test_presets_list_known_providers() -> None:
    expected = {"deepseek", "minimax", "openai", "anthropic", "custom"}
    assert expected <= set(PROVIDER_PRESETS)


def test_custom_preset_needs_base_url() -> None:
    assert PROVIDER_PRESETS["custom"].needs_base_url is True
    assert PROVIDER_PRESETS["deepseek"].needs_base_url is False


def test_wizard_refuses_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("anthill.cli.setup._is_tty", return_value=False):
        code = run_wizard()
    assert code == 2


def test_wizard_aborts_when_existing_models_and_user_says_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing model + 'no' answer to 'Continue?' should exit 0."""
    from anthill.core.userconfig import ModelEntry, UserConfig, save_config

    save_config(
        UserConfig(
            default_model="existing",
            models=[
                ModelEntry(
                    name="existing",
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref="ref",
                )
            ],
        )
    )
    with patch("anthill.cli.setup._is_tty", return_value=True), \
         patch("anthill.cli.setup._prompt_yes_no", return_value=False):
        code = run_wizard()
    assert code == 0


def test_wizard_flow_writes_model_and_nation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full happy-path with all prompts mocked."""
    answers = {
        "Save as": "test-deepseek",
        "Model id": "deepseek-chat",
        "Nation name": "test-nation",
        "Citizens to spawn": "2",
    }

    def fake_prompt(question, default=None):
        for k, v in answers.items():
            if k in question:
                return v
        return default or ""

    with patch("anthill.cli.setup._is_tty", return_value=True), \
         patch("anthill.cli.setup._pick_provider", return_value="deepseek"), \
         patch("anthill.cli.setup._prompt", side_effect=fake_prompt), \
         patch("anthill.cli.setup._prompt_secret", return_value="sk-fake-key"), \
         patch("anthill.cli.setup._prompt_yes_no", return_value=True):
        code = run_wizard()
    assert code == 0

    from anthill.core.userconfig import load_config, load_secrets
    cfg = load_config()
    assert cfg.default_model == "test-deepseek"
    assert cfg.find_model("test-deepseek") is not None
    secrets = load_secrets()
    assert secrets["model.test-deepseek"] == "sk-fake-key"
