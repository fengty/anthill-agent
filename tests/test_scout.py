"""Scout parser tests — no live LLM calls.

Two halves: strict-parse happy paths and fallback-on-injection paths.
"""

from __future__ import annotations

from anthill.core.scout import Scout


# --- Strict parse path ---------------------------------------------------


def test_parse_single_subtask() -> None:
    text = '{"plan": [{"task_type": "translate", "prompt": "Translate hello to French", "depends_on": []}]}'
    plan = Scout._parse(text)
    assert len(plan) == 1
    assert plan.subtasks[0].task_type == "translate"
    assert plan.subtasks[0].depends_on == []


def test_parse_multi_subtask_with_deps() -> None:
    text = """
    {"plan": [
        {"task_type": "read_pdf", "prompt": "Extract text", "depends_on": []},
        {"task_type": "summarize", "prompt": "Summarize text", "depends_on": ["read_pdf"]}
    ]}
    """
    plan = Scout._parse(text)
    assert len(plan) == 2
    assert plan.subtasks[1].depends_on == ["read_pdf"]


def test_parse_strips_code_fence() -> None:
    text = """```json
{"plan": [{"task_type": "explain", "prompt": "Explain X", "depends_on": []}]}
```"""
    plan = Scout._parse(text)
    assert len(plan) == 1
    assert plan.subtasks[0].task_type == "explain"


def test_parse_extracts_embedded_json() -> None:
    """When the model wraps JSON in prose, still extract it."""
    text = (
        "Sure, here is my plan:\n"
        '{"plan": [{"task_type": "x", "prompt": "do x", "depends_on": []}]}\n'
        "Hope this helps."
    )
    plan = Scout._parse(text)
    assert len(plan) == 1
    assert plan.subtasks[0].task_type == "x"


# --- Fallback path: injection-resistant degradation ---------------------


def test_fallback_when_model_returns_naked_word() -> None:
    """The v0.2.10 smoke-test bug: user asked 'reply with pong',
    the model returned 'pong' (not JSON). Should NOT raise — fall back
    to a single 'general' task."""
    plan = Scout._parse("pong", fallback_request="reply with exactly: pong")
    assert len(plan) == 1
    assert plan.subtasks[0].task_type == "general"
    assert "pong" in plan.subtasks[0].prompt


def test_fallback_when_response_is_prose() -> None:
    plan = Scout._parse(
        "I'm sorry, I cannot help with that.",
        fallback_request="actual user request",
    )
    assert len(plan) == 1
    assert plan.subtasks[0].prompt == "actual user request"


def test_fallback_on_empty_plan() -> None:
    """An empty plan was an error before; now degrade."""
    plan = Scout._parse('{"plan": []}', fallback_request="r")
    assert len(plan) == 1


def test_fallback_on_missing_task_type() -> None:
    text = '{"plan": [{"prompt": "do something", "depends_on": []}]}'
    plan = Scout._parse(text, fallback_request="r")
    assert len(plan) == 1
    assert plan.subtasks[0].task_type == "general"


def test_fallback_on_missing_prompt() -> None:
    text = '{"plan": [{"task_type": "summarize", "depends_on": []}]}'
    plan = Scout._parse(text, fallback_request="r")
    assert len(plan) == 1


def test_fallback_uses_text_when_no_request_given() -> None:
    """If caller forgot fallback_request, use the raw text."""
    plan = Scout._parse("just prose")
    assert len(plan) == 1
    assert "just prose" in plan.subtasks[0].prompt


# --- System prompt defenses ---------------------------------------------


def test_system_prompt_warns_against_injection() -> None:
    from anthill.core.scout import build_system_prompt
    prompt = build_system_prompt(None)
    assert "<user_request>" in prompt
    assert "IGNORE" in prompt or "ignore" in prompt
    assert "DATA" in prompt or "data" in prompt
