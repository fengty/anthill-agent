"""0.1.12 — multi-line input via `\"\"\"` heredoc.

Closes "pasting a code snippet with newlines auto-submits after the
first line." Users type `\"\"\"`, the REPL switches to a continuation
prompt, accumulates lines, and submits when a closing `\"\"\"` is seen.

Tests:
  1. Plain single-line input passes through unchanged
  2. `\"\"\"...\"\"\"` on one line strips both quotes and returns content
  3. Opening `\"\"\"` then content lines then closing `\"\"\"` accumulates
  4. Content on same line as opener is kept
  5. Trailing `\"\"\"` on a content line closes the block
  6. Internal blank lines are preserved (paragraph breaks survive)
  7. Leading whitespace is preserved (code indentation matters)
  8. EOF mid-block submits accumulated content
  9. Empty multi-line block returns empty string
"""

from __future__ import annotations

import pytest


def _drive(inputs, monkeypatch):
    """Pump a sequence of input() responses through _read_request_line."""
    from anthill.cli import repl

    seq = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(seq))
    return repl._read_request_line()


def test_single_line_passes_through(monkeypatch) -> None:
    result = _drive(["hello world"], monkeypatch)
    assert result == "hello world"


def test_single_line_is_stripped(monkeypatch) -> None:
    result = _drive(["  spaced  "], monkeypatch)
    assert result == "spaced"


def test_inline_triple_quote_pair(monkeypatch) -> None:
    """A complete `\"\"\"...\"\"\"` on one line is treated as multi-line content."""
    result = _drive(['"""hello world"""'], monkeypatch)
    assert result == "hello world"


def test_multiline_basic(monkeypatch) -> None:
    """Opener, two content lines, closer — accumulates with newlines."""
    result = _drive(
        ['"""', "first line", "second line", '"""'], monkeypatch
    )
    assert result == "first line\nsecond line"


def test_multiline_content_on_opener_line(monkeypatch) -> None:
    """`\"\"\"text` keeps `text` as the first content line."""
    result = _drive(
        ['"""def foo():', "    return 1", '"""'], monkeypatch
    )
    assert result == "def foo():\n    return 1"


def test_multiline_closer_on_content_line(monkeypatch) -> None:
    """Trailing `\"\"\"` after content closes the block cleanly."""
    result = _drive(
        ['"""', "first", 'last\"\"\"'], monkeypatch
    )
    assert result == "first\nlast"


def test_multiline_preserves_blank_lines(monkeypatch) -> None:
    """Internal empty lines stay — paragraph breaks matter."""
    result = _drive(
        ['"""', "para one", "", "para two", '"""'], monkeypatch
    )
    assert result == "para one\n\npara two"


def test_multiline_preserves_leading_whitespace(monkeypatch) -> None:
    """Code paste: indentation must survive."""
    result = _drive(
        ['"""', "    indented", "        more", '"""'], monkeypatch
    )
    assert result == "    indented\n        more"


def test_multiline_eof_submits_accumulated(monkeypatch) -> None:
    """Ctrl+D mid-block submits what we have so far (don't lose work)."""
    from anthill.cli import repl

    def fake_input(_prompt=""):
        # First call returns the opener; second raises EOFError.
        if not hasattr(fake_input, "calls"):
            fake_input.calls = 0
        fake_input.calls += 1
        if fake_input.calls == 1:
            return '"""'
        if fake_input.calls == 2:
            return "salvaged"
        raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    result = repl._read_request_line()
    assert result == "salvaged"


def test_multiline_ctrl_c_propagates(monkeypatch) -> None:
    """Ctrl+C in continuation lines bubbles up so the REPL can cancel."""
    from anthill.cli import repl

    def fake_input(_prompt=""):
        if not hasattr(fake_input, "calls"):
            fake_input.calls = 0
        fake_input.calls += 1
        if fake_input.calls == 1:
            return '"""'
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", fake_input)
    with pytest.raises(KeyboardInterrupt):
        repl._read_request_line()


def test_multiline_empty_block(monkeypatch) -> None:
    """`\"\"\"` immediately followed by closing `\"\"\"` returns empty string."""
    result = _drive(['"""', '"""'], monkeypatch)
    assert result == ""


def test_at_file_token_survives_multiline(monkeypatch) -> None:
    """Multi-line content containing `@file` references stays intact —
    expansion happens later in _handle_ask, not in input."""
    result = _drive(
        ['"""', "review @src/foo.py and", "@docs/bar.md please", '"""'], monkeypatch
    )
    assert "@src/foo.py" in result
    assert "@docs/bar.md" in result
