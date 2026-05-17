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
from anthill.cli.setup_cmd import run_wizard


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
    with patch("anthill.cli.setup_cmd._is_tty", return_value=False):
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
    with patch("anthill.cli.setup_cmd._is_tty", return_value=True), \
         patch("anthill.cli.setup_cmd._prompt_yes_no", return_value=False):
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

    with patch("anthill.cli.setup_cmd._is_tty", return_value=True), \
         patch("anthill.cli.setup_cmd._pick_provider", return_value="deepseek"), \
         patch("anthill.cli.setup_cmd._prompt", side_effect=fake_prompt), \
         patch(
             "anthill.cli.setup_cmd._prompt_model_id",
             return_value="deepseek-chat",
         ), \
         patch("anthill.cli.setup_cmd._prompt_int", return_value=2), \
         patch("anthill.cli.setup_cmd._prompt_secret", return_value="sk-fake-key"), \
         patch("anthill.cli.setup_cmd._prompt_yes_no", return_value=True):
        code = run_wizard()
    assert code == 0

    from anthill.core.userconfig import load_config, load_secrets
    cfg = load_config()
    assert cfg.default_model == "test-deepseek"
    assert cfg.find_model("test-deepseek") is not None
    secrets = load_secrets()
    assert secrets["model.test-deepseek"] == "sk-fake-key"


def test_prompt_int_reprompts_on_non_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo (e.g. a stray Chinese character) re-prompts, doesn't snap to default."""
    from anthill.cli.setup_cmd import _prompt_int

    answers = iter(["", "abc", "秦", "5"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    # Empty submit returns default; other three reject; "5" accepts.
    assert _prompt_int("Citizens", default=3, min_val=1, max_val=50) == 3

    answers2 = iter(["秦", "0", "999", "7"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers2))
    assert _prompt_int("Citizens", default=3, min_val=1, max_val=50) == 7


def test_prompt_model_id_warns_on_unknown_then_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing 'deepseek' instead of 'deepseek-chat' triggers a confirm step."""
    from anthill.cli.setup_cmd import _prompt_model_id

    # First call: user types a bad id, refuses confirm, then types the right one.
    answers = iter(["deepseek", "n", "deepseek-chat"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    result = _prompt_model_id(
        "Model id",
        default="deepseek-chat",
        known=("deepseek-chat", "deepseek-reasoner"),
    )
    assert result == "deepseek-chat"


def test_prompt_model_id_accepts_anything_when_no_known_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom-endpoint case: empty known tuple disables the allow-list."""
    from anthill.cli.setup_cmd import _prompt_model_id

    monkeypatch.setattr("builtins.input", lambda _prompt: "anything-goes")
    result = _prompt_model_id("Model id", default="x", known=())
    assert result == "anything-goes"


def test_deepseek_preset_has_known_models() -> None:
    """Guard the explicit allow-list that prevents the 'deepseek' typo bug."""
    preset = PROVIDER_PRESETS["deepseek"]
    assert "deepseek-chat" in preset.known_models
    assert "deepseek-reasoner" in preset.known_models
    assert "deepseek" not in preset.known_models  # the bug case
