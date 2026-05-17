"""0.1.18-prep — /model rm in the REPL.

`anthill model remove NAME` (the CLI command) already existed; the
gap was that users in the REPL had no easy way to delete a model
they'd misconfigured during setup. This patch adds:

- `/model` lists models with a 1-based index
- `/model use NAME-or-N` accepts an index
- `/model rm NAME-or-N` deletes one (with confirm prompt)
- `/model rm` (no args) walks every model interactively
- `/model rm NAME --yes` skips the confirm (for scripted flows)

Tests cover the handler behavior with mocked input + isolated home.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


def _seed_models() -> None:
    """Two configured models, deepseek as default."""
    from anthill.core.userconfig import (
        ModelEntry,
        UserConfig,
        save_config,
        upsert_secret,
    )

    upsert_secret("model.deepseek", "sk-d")
    upsert_secret("model.broken", "sk-b")
    save_config(
        UserConfig(
            default_model="deepseek",
            models=[
                ModelEntry(
                    name="deepseek",
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref="model.deepseek",
                ),
                ModelEntry(
                    name="broken",
                    provider="deepseek",
                    model="deepseek",  # the typo case from the original bug report
                    secret_ref="model.broken",
                ),
            ],
        )
    )


def test_model_rm_by_name_with_confirm(monkeypatch) -> None:
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_config

    _seed_models()
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    _handle_model_cmd("rm broken")
    cfg = load_config()
    assert cfg.find_model("broken") is None
    assert cfg.find_model("deepseek") is not None
    assert cfg.default_model == "deepseek"  # untouched


def test_model_rm_by_index(monkeypatch) -> None:
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_config

    _seed_models()
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    # Index 2 = "broken" in the seeded list.
    _handle_model_cmd("rm 2")
    cfg = load_config()
    assert cfg.find_model("broken") is None


def test_model_rm_with_yes_flag_skips_confirm(monkeypatch) -> None:
    """--yes bypasses the y/N prompt; useful for scripted flows."""
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_config

    _seed_models()
    asked = []

    def fail_input(_p):
        asked.append(_p)
        return "y"  # if reached, the test catches it via asked

    monkeypatch.setattr("builtins.input", fail_input)
    _handle_model_cmd("rm broken --yes")
    assert asked == []  # never prompted
    assert load_config().find_model("broken") is None


def test_model_rm_default_reassigns(monkeypatch) -> None:
    """Deleting the current default falls back to the first surviving model."""
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_config

    _seed_models()
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    _handle_model_cmd("rm deepseek")
    cfg = load_config()
    assert cfg.find_model("deepseek") is None
    # Only "broken" remains — it inherits default.
    assert cfg.default_model == "broken"


def test_model_rm_default_clears_when_last(monkeypatch) -> None:
    """Removing the last remaining model leaves default_model=None."""
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import (
        ModelEntry,
        UserConfig,
        load_config,
        save_config,
        upsert_secret,
    )

    upsert_secret("model.solo", "sk")
    save_config(
        UserConfig(
            default_model="solo",
            models=[
                ModelEntry(
                    name="solo",
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref="model.solo",
                )
            ],
        )
    )
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    _handle_model_cmd("rm solo")
    cfg = load_config()
    assert cfg.models == []
    assert cfg.default_model is None


def test_model_rm_confirm_no_keeps_model(monkeypatch) -> None:
    """Answering n / empty / anything-else keeps the model."""
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_config

    _seed_models()
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    _handle_model_cmd("rm broken")
    cfg = load_config()
    assert cfg.find_model("broken") is not None


def test_model_rm_interactive_walks_all(monkeypatch) -> None:
    """`/model rm` with no args asks per-model."""
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_config

    _seed_models()
    answers = iter(["n", "y"])  # keep deepseek, delete broken
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    _handle_model_cmd("rm")
    cfg = load_config()
    assert cfg.find_model("deepseek") is not None
    assert cfg.find_model("broken") is None


def test_model_rm_unknown_name_is_quiet(monkeypatch) -> None:
    """Bad input doesn't mutate config."""
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_config

    _seed_models()
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    _handle_model_cmd("rm nonexistent")
    cfg = load_config()
    # Both still there.
    assert cfg.find_model("deepseek") is not None
    assert cfg.find_model("broken") is not None


def test_model_use_accepts_index(monkeypatch) -> None:
    """/model use 2 switches default to whatever's at index 2."""
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_config

    _seed_models()
    _handle_model_cmd("use 2")
    assert load_config().default_model == "broken"


def test_model_add_delegates_to_wizard_helper(monkeypatch) -> None:
    """0.1.25 — `/model add` in REPL must call the same flow as
    `anthill setup` step 1, so REPL and setup never disagree on
    what a new model entry looks like.
    """
    from anthill.cli import repl as repl_mod
    from anthill.core.userconfig import load_config

    called = {}

    def fake_add(user_config):  # noqa: ANN001
        from anthill.core.userconfig import (
            ModelEntry,
            save_config,
            upsert_secret,
        )

        called["yes"] = True
        upsert_secret("model.via-repl", "sk-fake")
        user_config.models.append(
            ModelEntry(
                name="via-repl",
                provider="deepseek",
                model="deepseek-v4-pro",
                secret_ref="model.via-repl",
            )
        )
        save_config(user_config)
        return "via-repl", "model.via-repl"

    monkeypatch.setattr("anthill.cli.setup_cmd._add_model_interactive", fake_add)
    monkeypatch.setattr("anthill.cli.setup_cmd._is_tty", lambda: True)
    repl_mod._handle_model_cmd("add")
    assert called == {"yes": True}
    assert load_config().find_model("via-repl") is not None


def test_model_add_refuses_when_no_tty(monkeypatch) -> None:
    """Scripted environments (no TTY) get a clear refusal instead of hanging."""
    from anthill.cli import repl as repl_mod

    monkeypatch.setattr("anthill.cli.setup_cmd._is_tty", lambda: False)
    # If the helper got called, the test would mutate user config.
    # The assertion is just "no crash, no entry added".
    repl_mod._handle_model_cmd("add")
    from anthill.core.userconfig import load_config
    assert load_config().models == []  # _seed_models not called here


def test_model_rm_confirm_prompt_renders_no_literal_markup(
    monkeypatch, capsys
) -> None:
    """0.1.25 regression: input() can't render rich markup, so the
    confirm prompt had to be a console.print(..., end="") + bare
    input(""). Check that no `[cyan]` literal text appears in stdout.
    """
    from anthill.cli import repl as repl_mod

    _seed_models()
    monkeypatch.setattr("builtins.input", lambda _p="": "y")
    repl_mod._handle_model_cmd("rm broken")
    captured = capsys.readouterr()
    # Either the markup got rendered (no literal "[cyan]" in output)
    # OR the prompt was suppressed entirely. Either way: not literal.
    assert "[cyan]" not in captured.out
    assert "[/cyan]" not in captured.out
    assert "[dim]" not in captured.out
    """Deleting a model wipes its API-key secret too — no orphan secrets."""
    from anthill.cli.repl import _handle_model_cmd
    from anthill.core.userconfig import load_secrets

    _seed_models()
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    _handle_model_cmd("rm broken")
    secrets = load_secrets()
    assert "model.broken" not in secrets
    assert "model.deepseek" in secrets  # untouched
