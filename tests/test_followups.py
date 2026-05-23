"""0.2.17 — Rule-based follow-up suggestions after each ask.

The point of follow-ups: after a code-heavy answer, nudge toward
"want a working example?"; after long output, nudge toward "want
shorter?"; after a question with no clear next step, stay silent.

Tests focus on the BEHAVIOR contracts that matter at the REPL:
empty in → empty out, hard ceiling at 2, no duplicates. The
specific Chinese hint wording isn't worth testing — we can
rephrase any of those without changing UX.
"""

from __future__ import annotations

from anthill.core.followups import format_followup_line, suggest_followups


def test_empty_output_returns_no_hints() -> None:
    """No answer → nothing to follow up on. Most important contract:
    we don't print '💡 想要...?' on an empty response."""
    assert suggest_followups("hi", "") == []
    assert suggest_followups("hi", "   ") == []


def test_cap_at_2_hints_no_duplicates() -> None:
    """The REPL line budget is tight. Multiple rules firing must
    not flood the user with hints, and we shouldn't repeat the
    same hint twice."""
    # Long output + code + definition ask — three rules want to fire.
    long_code = "Long thing is\n```\n" + ("x" * 2500) + "\n```"
    hints = suggest_followups("什么是 long thing", long_code)
    assert len(hints) <= 2
    assert len(hints) == len(set(hints))


def test_some_hint_appears_for_typical_ask() -> None:
    """A 'definition ask + code in output' is the canonical case
    where follow-ups are useful. We don't assert WHICH hint comes
    out (rephrasing is fine) — just that the heuristic fires."""
    hints = suggest_followups(
        "什么是 docker compose",
        "Docker Compose 用 YAML 定义多容器服务.\n\n```yaml\nversion: '3'\n```",
    )
    assert len(hints) >= 1


def test_format_line_visual_contract() -> None:
    """Empty → empty string. Non-empty → the line starts with
    a 💡 marker so it's recognizable as a hint, not an answer."""
    assert format_followup_line([]) == ""
    line = format_followup_line(["A", "B"])
    assert line.startswith("💡 ")
    assert "A" in line and "B" in line
