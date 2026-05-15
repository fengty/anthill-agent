"""Scout decomposition tests — parser only, no live LLM calls."""

from __future__ import annotations

import pytest

from anthill.core.scout import Scout


def test_parse_single_subtask() -> None:
    text = '{"plan": [{"task_type": "translate", "prompt": "Translate hello to Chinese", "depends_on": []}]}'
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


def test_parse_rejects_non_json() -> None:
    with pytest.raises(RuntimeError, match="non-JSON"):
        Scout._parse("just some prose, no json here")


def test_parse_rejects_empty_plan() -> None:
    with pytest.raises(RuntimeError, match="empty or wrong shape"):
        Scout._parse('{"plan": []}')


def test_parse_rejects_missing_task_type() -> None:
    text = '{"plan": [{"prompt": "do something", "depends_on": []}]}'
    with pytest.raises(RuntimeError, match="missing task_type"):
        Scout._parse(text)


def test_parse_rejects_missing_prompt() -> None:
    text = '{"plan": [{"task_type": "summarize", "depends_on": []}]}'
    with pytest.raises(RuntimeError, match="missing prompt"):
        Scout._parse(text)
