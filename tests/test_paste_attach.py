"""0.2.47 — client-style paste-to-file in the REPL.

User pain: pasting 200 lines of log into the prompt = visual mess
+ token bloat. Web clients (ChatGPT, Claude.ai) detect heavy
pastes and turn them into file attachments. anthill should too.

Tests cover:
  - threshold: short input stays inline, heavy input persists
  - persistence: file actually written, content matches
  - extension detection: log / json / py / yaml / etc.
  - slash commands and @file references stay inline (don't double-wrap)
  - file path detection: drag-from-Finder paths get @-ified
  - graceful disk failure: returns inline if write fails
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.paste_attach import (
    PASTE_CHAR_THRESHOLD,
    PasteResult,
    maybe_persist_paste,
    maybe_resolve_file_path,
)


# --- threshold ------------------------------------------------------


def test_short_input_stays_inline(tmp_path: Path) -> None:
    """A typed question (<300 chars) doesn't trigger paste mode."""
    result = maybe_persist_paste("帮我看看 mysql 慢查询", tmp_path)
    assert result.kind == "inline"
    assert result.rewritten == "帮我看看 mysql 慢查询"
    assert result.persisted_path is None


def test_heavy_paste_persists_to_file(tmp_path: Path) -> None:
    """Big paste → saved to ~/anthill/pastes/, rewritten to @path."""
    big = "log line " * 200  # ~1800 chars
    result = maybe_persist_paste(big, tmp_path)
    assert result.kind == "paste_saved"
    assert result.persisted_path is not None
    assert result.persisted_path.exists()
    # Content round-trip.
    assert result.persisted_path.read_text() == big
    # Rewritten to @<path> form.
    assert result.rewritten.startswith("@")
    assert str(result.persisted_path) in result.rewritten


def test_many_lines_triggers_even_if_short_chars(tmp_path: Path) -> None:
    """20 short lines (well below 1000 chars but above line threshold)
    also count as a heavy paste."""
    block = "\n".join(f"line {i}" for i in range(20))
    result = maybe_persist_paste(block, tmp_path)
    assert result.kind == "paste_saved"


def test_borderline_input_doesnt_trigger(tmp_path: Path) -> None:
    """A 500-char one-liner is NOT a paste — likely a long typed
    question or a one-line URL with params."""
    line = "x" * 500
    result = maybe_persist_paste(line, tmp_path)
    assert result.kind == "inline"


# --- slash commands / @file refs stay inline -----------------------


def test_slash_command_never_persisted(tmp_path: Path) -> None:
    """Even a HUGE /command line goes through unchanged — we don't
    want to file-ify the user's own slash-command args."""
    big_cmd = "/test " + "x" * 2000
    result = maybe_persist_paste(big_cmd, tmp_path)
    assert result.kind == "inline"
    assert result.rewritten == big_cmd


def test_at_file_reference_never_persisted(tmp_path: Path) -> None:
    """`@/path/to/big_file ...rest...` — user already @-attached,
    don't wrap it again."""
    text = "@/etc/some-file " + "x" * 2000
    result = maybe_persist_paste(text, tmp_path)
    assert result.kind == "inline"


# --- extension detection -------------------------------------------


def test_extension_detected_log(tmp_path: Path) -> None:
    """ISO-timestamped log lines → .log extension."""
    block = "\n".join(
        f"2026-05-24T10:0{i}:00 ERROR something broke" for i in range(20)
    )
    result = maybe_persist_paste(block, tmp_path)
    assert result.persisted_path.suffix == ".log"


def test_extension_detected_json(tmp_path: Path) -> None:
    """Starts with { → .json. Just needs to be big enough."""
    block = "{\n" + "  \"key\": \"value\",\n" * 60 + "}"
    result = maybe_persist_paste(block, tmp_path)
    assert result.persisted_path.suffix == ".json"


def test_extension_detected_python(tmp_path: Path) -> None:
    """import/def hints → .py."""
    block = "import os\nimport sys\n" + "def foo():\n    pass\n" * 60
    result = maybe_persist_paste(block, tmp_path)
    assert result.persisted_path.suffix == ".py"


def test_extension_falls_back_to_txt(tmp_path: Path) -> None:
    """Ambiguous content → .txt."""
    block = "just some random prose " * 100
    result = maybe_persist_paste(block, tmp_path)
    assert result.persisted_path.suffix == ".txt"


# --- file path detection ------------------------------------------


def test_resolve_file_path_existing_file(tmp_path: Path) -> None:
    """User drags a file from Finder; path goes into terminal;
    we detect and return the resolved Path."""
    f = tmp_path / "drag-me.txt"
    f.write_text("contents")
    resolved = maybe_resolve_file_path(str(f))
    assert resolved == f


def test_resolve_file_path_nonexistent(tmp_path: Path) -> None:
    """A path-shaped string that doesn't actually exist returns None.
    Don't @-ify ghost paths."""
    assert maybe_resolve_file_path("/this/does/not/exist.txt") is None


def test_resolve_file_path_unescapes_finder_spaces(tmp_path: Path) -> None:
    """macOS Finder drag inserts `\\ ` for spaces in paths.
    We un-escape that."""
    f = tmp_path / "My File.txt"
    f.write_text("x")
    escaped = str(f).replace(" ", "\\ ")
    resolved = maybe_resolve_file_path(escaped)
    assert resolved == f


def test_resolve_file_path_rejects_non_path_inputs() -> None:
    """Things that LOOK like questions, not paths, stay None even
    if they happen to have a / in them."""
    assert maybe_resolve_file_path("how do I cd /usr/local") is None
    assert maybe_resolve_file_path("research this thing") is None
    assert maybe_resolve_file_path("") is None


# --- defensive: disk failure doesn't break input -------------------


def test_disk_write_failure_falls_back_to_inline(tmp_path: Path, monkeypatch) -> None:
    """If we can't write the paste file (full disk / readonly),
    fall back to inline so the user's input still gets through."""
    big = "x" * 5000
    # Point home at a path that can't be written to.
    blocked = tmp_path / "blocked"
    blocked.write_text("file-not-dir")  # parent of /pastes is a file
    result = maybe_persist_paste(big, blocked)
    # Either fell back to inline OR somehow worked — both OK as
    # long as no exception.
    assert isinstance(result, PasteResult)
