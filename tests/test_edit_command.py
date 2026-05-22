"""0.2.15 — `/edit` invokes $EDITOR for long prompts.

REPL one-liners punish multi-paragraph asks. Triple-quote multi-line
helps but every paste fights readline. `/edit` hands vim / nano /
code the wheel: compose freely, save, content becomes the next ask.

Tests cover the editor-resolution + content-handling logic. We
shadow `subprocess.call` so the test never actually launches a real
editor — it just writes content to the tmp file the helper passed
in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

import pytest


def _fake_editor_writes(content: str):
    """Return a callable that emulates an editor: writes `content`
    to the file path passed as argv[-1] and returns 0."""

    def runner(cmd, *_args, **_kwargs):
        tmpfile = cmd[-1]
        Path(tmpfile).write_text(content, encoding="utf-8")
        return 0

    return runner


def test_compose_in_editor_returns_saved_content(monkeypatch) -> None:
    from anthill.cli.repl import _compose_in_editor

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr(
        "subprocess.call",
        _fake_editor_writes("Hello, this is my long ask."),
    )
    result = _compose_in_editor()
    assert result == "Hello, this is my long ask."


def test_compose_in_editor_strips_comment_header(monkeypatch) -> None:
    """The helper prepends a `#`-comment header. The user might
    leave it in; we strip every `#` line."""
    from anthill.cli.repl import _compose_in_editor

    monkeypatch.setenv("EDITOR", "fake-editor")
    raw = (
        "# Compose your ask below.\n"
        "# Lines starting with '#' are ignored.\n"
        "#\n"
        "What is mysql HA topology?\n"
        "Compare master-master vs MGR.\n"
    )
    monkeypatch.setattr("subprocess.call", _fake_editor_writes(raw))
    result = _compose_in_editor()
    assert result == "What is mysql HA topology?\nCompare master-master vs MGR."


def test_compose_in_editor_blank_returns_none(monkeypatch) -> None:
    """User saved empty file → cancel signal."""
    from anthill.cli.repl import _compose_in_editor

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr(
        "subprocess.call", _fake_editor_writes("# only comments\n#\n")
    )
    assert _compose_in_editor() is None


def test_compose_in_editor_seed_is_prefilled(monkeypatch) -> None:
    """`/edit fix the bug` opens the editor with 'fix the bug' inside.
    The user keeps editing; if they save unchanged, the seed comes
    back as the result."""
    from anthill.cli.repl import _compose_in_editor

    captured = {}

    def runner(cmd, *_args, **_kwargs):
        # Read what was in the file when the "editor" opened.
        captured["initial"] = Path(cmd[-1]).read_text(encoding="utf-8")
        # User saves it unchanged.
        return 0

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.setattr("subprocess.call", runner)
    result = _compose_in_editor(initial="fix the bug")

    assert "fix the bug" in captured["initial"]
    assert result == "fix the bug"


def test_compose_in_editor_no_editor_available(monkeypatch) -> None:
    """When neither $EDITOR nor a fallback is found we should
    return None without crashing."""
    from anthill.cli.repl import _compose_in_editor

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    # Force every `which` probe to fail.
    import subprocess as _sp

    def fake_run(*_args, **_kwargs):
        raise _sp.CalledProcessError(1, "which")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _compose_in_editor() is None


def test_compose_in_editor_respects_editor_with_args(monkeypatch) -> None:
    """$EDITOR='code --wait' is common; shlex.split lets us
    handle that without quoting nightmares."""
    from anthill.cli.repl import _compose_in_editor

    received_cmd = {}

    def runner(cmd, *_args, **_kwargs):
        received_cmd["cmd"] = list(cmd)
        Path(cmd[-1]).write_text("done", encoding="utf-8")
        return 0

    monkeypatch.setenv("EDITOR", "code --wait")
    monkeypatch.setattr("subprocess.call", runner)
    result = _compose_in_editor()
    assert result == "done"
    assert received_cmd["cmd"][0] == "code"
    assert received_cmd["cmd"][1] == "--wait"


# --- /edit registered in completion + help ----------------------------


def test_edit_in_slash_completion() -> None:
    from anthill.cli.completion import KNOWN_SLASH_COMMANDS

    assert "/edit" in KNOWN_SLASH_COMMANDS
    assert "/e" in KNOWN_SLASH_COMMANDS


def test_edit_documented_in_help() -> None:
    from anthill.cli.repl import HELP_TEXT

    assert "/edit" in HELP_TEXT
    assert "$EDITOR" in HELP_TEXT


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
