"""0.2.10 — Scout shouldn't reach for `clarify` on direct questions.

Real session data: '你如何和我的飞书对接的？' got planned as
`[clarify]` — a single-subtask plan that just asks the user back.
The model then said "您想了解 Anthill 接入飞书 还是 中间件监控
推送到飞书？" — making the user pick before doing any work.

This is the failure mode the rule update prevents. We can't actually
run a live LLM in tests, but we CAN assert the rule text is in the
Scout system prompt, and that the rule documents the patterns we
care about.
"""

from __future__ import annotations

from anthill.core.scout import (
    SCOUT_SYSTEM_PROMPT_TEMPLATE,
    build_system_prompt,
)


def test_scout_prompt_contains_clarify_restraint_section() -> None:
    """A dedicated section in the prompt must exist — it's how we
    keep this rule discoverable for whoever next reads the prompt."""
    assert "when NOT to plan a `clarify` subtask" in SCOUT_SYSTEM_PROMPT_TEMPLATE


def test_scout_prompt_documents_explain_default() -> None:
    """The fallback when uncertain should be `explain` / `answer`,
    not `clarify`."""
    text = SCOUT_SYSTEM_PROMPT_TEMPLATE.lower()
    assert "explain" in text or "answer" in text


def test_scout_prompt_warns_against_single_clarify_plan() -> None:
    """The smoking gun from the session log was a plan that was
    JUST one clarify subtask. The prompt should call that out."""
    text = SCOUT_SYSTEM_PROMPT_TEMPLATE
    assert "ONLY one subtask of type `clarify`" in text


def test_scout_prompt_lists_self_referential_patterns() -> None:
    """The patterns that should NOT trigger clarify need to be
    explicit — 你能... / 你如何... / anthill 怎么... etc."""
    text = SCOUT_SYSTEM_PROMPT_TEMPLATE
    # At least one Chinese self-ref pattern documented.
    assert "你能" in text or "你如何" in text or "anthill" in text.lower()


def test_built_system_prompt_includes_restraint() -> None:
    """`build_system_prompt` is what actually goes to Scout. Verify
    the restraint section survives the formatting step (vocab section
    injected, etc.)."""
    prompt = build_system_prompt(known_task_types=["research", "analyze"])
    assert "when NOT to plan a `clarify` subtask" in prompt
    assert "ATTEMPT the answer" in prompt


def test_built_system_prompt_empty_vocab() -> None:
    """No prior task_types yet — restraint section should STILL
    appear (it's not vocab-dependent)."""
    prompt = build_system_prompt(known_task_types=None)
    assert "when NOT to plan a `clarify` subtask" in prompt


def test_prompt_documents_guess_then_offer_correction() -> None:
    """The directive's positive guidance: pick a likely interpretation,
    answer it, then briefly mention the alternative — instead of
    making the user disambiguate before any work happens."""
    text = SCOUT_SYSTEM_PROMPT_TEMPLATE
    assert "best guess" in text or "more likely" in text
