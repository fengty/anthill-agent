"""0.2.22 — external tool detection.

When the user asks "你如何和我的飞书对接的？" anthill used to
answer "I'm just an AI model, no integration capability." That's
the truth for built-in tools — but the user has lark-cli installed.
We just didn't tell the citizens.

This module scans $PATH for a curated list of useful CLIs and
makes them visible in the self-context block.
"""

from __future__ import annotations

import shutil
from unittest import mock

from anthill.core.tool_detect import (
    DetectedTool,
    detect_tools,
    format_tools_block,
)


def test_detect_returns_only_present_tools() -> None:
    """A tool that resolves on $PATH appears; one that doesn't is
    skipped. We mock shutil.which to make this deterministic."""

    def fake_which(name: str) -> str | None:
        if name == "git":
            return "/usr/bin/git"
        if name == "lark-cli":
            return "/usr/local/bin/lark-cli"
        return None

    # Clear lru_cache so the mock takes effect.
    detect_tools.cache_clear()
    try:
        with mock.patch("anthill.core.tool_detect.shutil.which", side_effect=fake_which):
            tools = detect_tools()
    finally:
        detect_tools.cache_clear()

    names = {t.name for t in tools}
    assert "git" in names
    assert "lark-cli" in names
    # Tools NOT in the fake_which should NOT appear.
    assert "docker" not in names
    assert "kubectl" not in names


def test_format_empty_when_no_tools() -> None:
    """No tools detected → empty string. Don't pollute the prompt
    with an empty header."""
    assert format_tools_block(tools=()) == ""


def test_format_includes_path_and_description() -> None:
    """The block must include both the resolved path and the
    one-line description — the model needs path to invoke,
    description to know when to invoke."""
    tools = (
        DetectedTool(name="lark-cli", path="/opt/lark-cli", description="飞书 CLI"),
    )
    block = format_tools_block(tools)
    assert "lark-cli" in block
    assert "/opt/lark-cli" in block
    assert "飞书 CLI" in block
    # And the marker contract is reinforced.
    assert "[[bash:" in block


def test_real_scan_finds_at_least_one_tool() -> None:
    """End-to-end: on any dev machine running this test, AT LEAST
    ONE tool from the curated list should be on PATH (git almost
    certainly is). This catches regressions where the scanner
    silently returns []."""
    detect_tools.cache_clear()
    try:
        tools = detect_tools()
    finally:
        detect_tools.cache_clear()
    # If git isn't on the test machine, this might fail — but git
    # is in every CI environment we care about.
    if shutil.which("git"):
        names = {t.name for t in tools}
        assert "git" in names, "git is on $PATH but detect_tools missed it"


def test_self_context_includes_detected_tools() -> None:
    """End-to-end: self_context_block surfaces the detected tools
    so citizens see them when answering self-referential asks."""
    from anthill.core.self_context import self_context_block

    # Force a known tool set.
    detect_tools.cache_clear()
    try:
        with mock.patch(
            "anthill.core.tool_detect.shutil.which",
            side_effect=lambda n: "/opt/lark-cli" if n == "lark-cli" else None,
        ):
            block = self_context_block(None)
    finally:
        detect_tools.cache_clear()

    # The lark-cli line is now in the self-context.
    assert "lark-cli" in block
    assert "/opt/lark-cli" in block
