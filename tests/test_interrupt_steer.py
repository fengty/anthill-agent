"""0.1.36 — interrupt-and-steer after Ctrl+C during an ask.

Closes the `experience.md` §4 "No interrupt-and-steer" ❌. Before
this patch Ctrl+C during a streaming ask just printed "(cancelled)"
and dropped back to the prompt — the user lost the agent's
progress AND had to retype the whole question to redirect.

After: Ctrl+C → tiny menu → either cancel cleanly OR type a new
instruction that gets framed as a follow-up correction and fired
as a fresh ask without leaving the REPL.

Tests cover the pure helper (`_prompt_steer_choice`); the
asyncio.run / KeyboardInterrupt integration is left for manual
verification since the REPL loop isn't easily unit-testable.
"""

from __future__ import annotations


def _drive(inputs, monkeypatch):
    """Pump a sequence of `input()` responses through the helper."""
    from anthill.cli import repl

    seq = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(seq))
    return repl._prompt_steer_choice("original request text")


# --- cancel paths --------------------------------------------------------


def test_empty_choice_cancels(monkeypatch) -> None:
    """Hitting Enter at the menu = cancel (the cheap default)."""
    assert _drive([""], monkeypatch) is None


def test_c_choice_cancels(monkeypatch) -> None:
    assert _drive(["c"], monkeypatch) is None


def test_cancel_word_cancels(monkeypatch) -> None:
    """Anything that isn't 'r' / 'redirect' is treated as cancel."""
    assert _drive(["nope"], monkeypatch) is None
    assert _drive(["x"], monkeypatch) is None
    assert _drive(["whatever"], monkeypatch) is None


def test_ctrl_c_at_menu_cancels(monkeypatch) -> None:
    """Pressing Ctrl+C AT the menu (after the first one) is also
    a cancel — we never want to trap the user."""
    from anthill.cli import repl

    def kbi(_p=""):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", kbi)
    assert repl._prompt_steer_choice("anything") is None


def test_eof_at_menu_cancels(monkeypatch) -> None:
    """Ctrl+D at the menu cancels too."""
    from anthill.cli import repl

    def eof(_p=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert repl._prompt_steer_choice("anything") is None


# --- redirect happy paths -----------------------------------------------


def test_redirect_returns_new_instruction(monkeypatch) -> None:
    """User typed 'r', then a follow-up — helper returns the follow-up."""
    result = _drive(["r", "actually focus on Chinese AI tools"], monkeypatch)
    assert result == "actually focus on Chinese AI tools"


def test_redirect_full_word(monkeypatch) -> None:
    """'redirect' (full word) also enters the redirect flow."""
    result = _drive(["redirect", "go shorter please"], monkeypatch)
    assert result == "go shorter please"


def test_redirect_case_insensitive(monkeypatch) -> None:
    result = _drive(["R", "tighten it up"], monkeypatch)
    assert result == "tighten it up"


def test_redirect_strips_whitespace(monkeypatch) -> None:
    result = _drive(["r", "   trim trailing   "], monkeypatch)
    assert result == "trim trailing"


# --- redirect with empty / interrupted follow-up ------------------------


def test_empty_redirect_text_cancels(monkeypatch) -> None:
    """Typed 'r' then hit Enter — bail to cancel, don't loop pestering."""
    result = _drive(["r", ""], monkeypatch)
    assert result is None


def test_ctrl_c_during_redirect_text_cancels(monkeypatch) -> None:
    """Ctrl+C while typing the redirect text → cancel."""
    from anthill.cli import repl

    calls = {"n": 0}

    def fake_input(_p=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return "r"
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", fake_input)
    assert repl._prompt_steer_choice("x") is None


def test_eof_during_redirect_text_cancels(monkeypatch) -> None:
    from anthill.cli import repl

    calls = {"n": 0}

    def fake_input(_p=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return "redirect"
        raise EOFError
    monkeypatch.setattr("builtins.input", fake_input)
    assert repl._prompt_steer_choice("x") is None
