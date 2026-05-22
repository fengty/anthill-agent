"""0.2.4 — final-output Markdown rendering.

User feedback on a real 0.2.3 session: '好丑啊'. Reason: model output
was full of markdown (## headers, | tables |, ``` code fences, bullets)
but anthill printed it as plain text. Rich's Markdown widget renders
all of that properly; we just weren't using it.

These tests verify the rendering helper itself — that it:
  - doesn't raise on weird input
  - falls back to plain text when Markdown parsing errors
  - skips empty / whitespace-only input
  - produces output a user can see (writes to the captured console)
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console


def _render_with(text: str) -> str:
    """Run _print_final_output against a fresh string-capturing console
    and return what it would have shown. Patches the module-level
    `console` so our helper writes there."""
    import anthill.cli.repl as repl_mod
    buf = StringIO()
    fake_console = Console(file=buf, force_terminal=False, width=80)
    original = repl_mod.console
    repl_mod.console = fake_console
    try:
        repl_mod._print_final_output(text)
    finally:
        repl_mod.console = original
    return buf.getvalue()


def test_empty_input_renders_nothing() -> None:
    assert _render_with("") == ""
    assert _render_with("   \n\n  ") == ""


def test_plain_text_renders_text() -> None:
    out = _render_with("Just plain output, no markdown.")
    assert "Just plain output" in out


def test_markdown_header_renders() -> None:
    """## headers must produce visible heading text. Rich's Markdown
    formats them; the literal '##' should NOT appear in the
    rendered output (it becomes styled text)."""
    text = "## Findings\n\nSome content."
    out = _render_with(text)
    # The text 'Findings' is in the output, but the '##' marker
    # itself shouldn't be (it gets eaten by the Markdown parser).
    assert "Findings" in out
    assert "##" not in out


def test_bullet_list_renders() -> None:
    text = "- alpha\n- beta\n- gamma"
    out = _render_with(text)
    # All three items still readable. Rich may use • or - depending
    # on theme.
    for item in ("alpha", "beta", "gamma"):
        assert item in out


def test_code_fence_renders() -> None:
    """Code fences should produce a code block, not raw backticks.

    Rich's Markdown formats the inner code; the literal opening
    ``` triple-backticks shouldn't appear in the output.
    """
    text = "Here's some code:\n\n```python\ndef foo():\n    pass\n```"
    out = _render_with(text)
    assert "def foo" in out
    # The backticks themselves should NOT appear as literal text
    # (they're consumed by the Markdown parser).
    assert "```" not in out


def test_table_renders() -> None:
    """Pipe-style tables should produce real columns, not the raw
    ASCII pipes that 0.2.3 leaked."""
    text = (
        "| Field | Value |\n"
        "|-------|-------|\n"
        "| name  | alice |\n"
        "| age   | 30    |\n"
    )
    out = _render_with(text)
    # The data is present in some rendered form.
    assert "name" in out
    assert "alice" in out
    assert "age" in out
    # The raw separator '|-------|' is gone — that was the eyesore.
    assert "|-------" not in out


def test_malformed_markdown_doesnt_crash() -> None:
    """Some model outputs have unbalanced fences / nested weirdness.
    The helper must NEVER raise."""
    for weird in [
        "```unclosed code fence",
        "## header with [unclosed link",
        "| missing | columns",
        "text with \x00 null byte",
    ]:
        # Should not raise.
        out = _render_with(weird)
        # And something must be visible (the helper shouldn't drop
        # output silently on malformed input).
        assert out  # non-empty


def test_chinese_content_renders() -> None:
    """The motivating screenshot was Chinese. Rich handles unicode
    fine; verify the test confirms it."""
    text = "## 中间件部署方案\n\n阿里云 RDS for MySQL 是好选择。"
    out = _render_with(text)
    assert "中间件部署方案" in out
    assert "阿里云" in out


@pytest.mark.parametrize(
    "marker",
    ["✅", "❌", "·", "→", "🍪", "📚"],
)
def test_emoji_passes_through(marker: str) -> None:
    """Emojis used in anthill UI hints (✅/❌/📚) should survive
    markdown rendering."""
    out = _render_with(f"Status: {marker} done")
    assert marker in out
