"""Executor tests — topological sort, cycle detection, context passing.

We avoid live LLM calls. For execute_plan, we stub the nation's run method.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from anthill.core.executor import (
    ExecutorError,
    build_context_block,
    execute_plan,
    topological_order,
)
from anthill.core.scout import Plan, Subtask


def _plan(*specs: tuple[str, list[str]]) -> Plan:
    """Helper: build a Plan from (task_type, depends_on) tuples."""
    return Plan(
        subtasks=[
            Subtask(task_type=tt, prompt=f"do {tt}", depends_on=list(deps))
            for tt, deps in specs
        ]
    )


def test_single_subtask_is_trivially_ordered() -> None:
    p = _plan(("translate", []))
    assert topological_order(p) == [0]


def test_independent_subtasks_keep_plan_order() -> None:
    p = _plan(("a", []), ("b", []), ("c", []))
    assert topological_order(p) == [0, 1, 2]


def test_chain_orders_by_dependency() -> None:
    p = _plan(("research", []), ("outline", ["research"]), ("draft", ["outline"]))
    assert topological_order(p) == [0, 1, 2]


def test_dependency_can_be_anywhere_earlier() -> None:
    # 'draft' depends on 'research' even though 'outline' sits between them.
    p = _plan(("research", []), ("outline", []), ("draft", ["research"]))
    order = topological_order(p)
    # research must come before draft; outline is unconstrained relative to others
    assert order.index(0) < order.index(2)


def test_missing_dependency_raises() -> None:
    p = _plan(("draft", ["nonexistent"]))
    with pytest.raises(ExecutorError, match="no other subtask"):
        topological_order(p)


def test_forward_dependency_raises() -> None:
    # 'draft' depends on 'review' but review appears AFTER it.
    p = _plan(("draft", ["review"]), ("review", []))
    with pytest.raises(ExecutorError, match="no earlier subtask"):
        topological_order(p)


def test_depends_on_latest_matching_type() -> None:
    """When two subtasks share a type, depends_on resolves to the latest one."""
    p = _plan(
        ("research", []),
        ("research", []),
        ("draft", ["research"]),
    )
    # 'draft' should depend on subtask index 1, not 0.
    # The executor would topologically order as [0, 1, 2] and draft only needs #1.
    order = topological_order(p)
    assert order.index(1) < order.index(2)


def test_build_context_block_empty_for_no_deps() -> None:
    sub = Subtask(task_type="translate", prompt="hello", depends_on=[])
    assert build_context_block(sub, {}) == ""


@dataclass
class _FakeResult:
    output: str


def test_build_context_block_formats_dependencies() -> None:
    sub = Subtask(task_type="summarize", prompt="now summarize", depends_on=["research"])
    completed: dict = {"research": _FakeResult(output="findings A")}
    block = build_context_block(sub, completed)
    assert "Previous results" in block
    assert "[research]" in block
    assert "findings A" in block
    assert block.endswith("---\n\n")


def test_build_context_block_multiple_deps() -> None:
    sub = Subtask(task_type="merge", prompt="combine", depends_on=["a", "b"])
    completed: dict = {
        "a": _FakeResult(output="alpha"),
        "b": _FakeResult(output="beta"),
    }
    block = build_context_block(sub, completed)
    assert "alpha" in block
    assert "beta" in block


class _FakeNation:
    """Minimal nation stub: records the prompt each subtask actually sees."""

    def __init__(self, outputs: dict[str, str]) -> None:
        self._outputs = outputs
        self.calls: list[tuple[str, str]] = []  # (task_type, prompt seen)

    async def run(self, task_type: str, prompt: str):  # noqa: ANN201 - test stub
        self.calls.append((task_type, prompt))
        return _FakeResult(output=self._outputs.get(task_type, f"<{task_type}>"))


@pytest.mark.asyncio
async def test_execute_plan_passes_context_downstream() -> None:
    p = _plan(("research", []), ("draft", ["research"]))
    nation = _FakeNation({"research": "the moon is far"})
    results = await execute_plan(p, nation)  # type: ignore[arg-type]

    assert len(results) == 2
    # The 'draft' subtask must have seen 'research' output in its prompt.
    draft_prompt = next(p for tt, p in nation.calls if tt == "draft")
    assert "the moon is far" in draft_prompt
    assert "[research]" in draft_prompt


@pytest.mark.asyncio
async def test_execute_plan_no_context_when_no_deps() -> None:
    p = _plan(("explain", []))
    nation = _FakeNation({"explain": "ok"})
    await execute_plan(p, nation)  # type: ignore[arg-type]
    _, prompt = nation.calls[0]
    assert "Previous results" not in prompt
