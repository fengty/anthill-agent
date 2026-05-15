"""Executor tests — topological sort, retries, skip-on-failure, context.

We avoid live LLM calls. For execute_plan, we stub the nation's run method.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from anthill.core.executor import (
    ExecutorError,
    RetryPolicy,
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
    """Mimics agent.TaskResult for executor tests."""

    output: str
    success_score: float = 1.0
    agent_id: str = "ant-fake"
    task_type: str = ""
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    task_id: str = "task-fake"


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
    """Minimal nation stub.

    Tracks every call, supports per-task-type scripted outcomes including
    failure sequences (e.g. fail twice then succeed). Honors `forbid` so
    we can verify the executor really rotates citizens on retry.
    """

    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        agents: list[str] | None = None,
        scripts: dict[str, list[float]] | None = None,
    ) -> None:
        self._outputs = outputs or {}
        self._agents = agents or ["ant-1", "ant-2", "ant-3"]
        self._scripts = scripts or {}
        self._counters: dict[str, int] = {}
        self.calls: list[tuple[str, str, frozenset[str]]] = []

    async def run(self, task_type: str, prompt: str, *, forbid=None):  # noqa: ANN201
        forbid_set = frozenset(forbid or set())
        self.calls.append((task_type, prompt, forbid_set))

        available = [a for a in self._agents if a not in forbid_set]
        if not available:
            raise RuntimeError("no citizens available")
        agent_id = available[0]

        attempt_idx = self._counters.get(task_type, 0)
        self._counters[task_type] = attempt_idx + 1

        # If a script exists for this task_type, use the next score in it;
        # otherwise default to success.
        script = self._scripts.get(task_type, [])
        score = script[attempt_idx] if attempt_idx < len(script) else 1.0

        output = self._outputs.get(task_type, f"<{task_type}>") if score > 0 else "[error]"
        return _FakeResult(
            output=output,
            success_score=score,
            agent_id=agent_id,
            task_type=task_type,
        )


@pytest.mark.asyncio
async def test_execute_plan_passes_context_downstream() -> None:
    p = _plan(("research", []), ("draft", ["research"]))
    nation = _FakeNation({"research": "the moon is far"})
    outcomes = await execute_plan(p, nation)  # type: ignore[arg-type]

    assert len(outcomes) == 2
    assert all(o.status == "ok" for o in outcomes)
    draft_prompt = next(p for tt, p, _ in nation.calls if tt == "draft")
    assert "the moon is far" in draft_prompt
    assert "[research]" in draft_prompt


@pytest.mark.asyncio
async def test_execute_plan_no_context_when_no_deps() -> None:
    p = _plan(("explain", []))
    nation = _FakeNation({"explain": "ok"})
    await execute_plan(p, nation)  # type: ignore[arg-type]
    _, prompt, _ = nation.calls[0]
    assert "Previous results" not in prompt


@pytest.mark.asyncio
async def test_retry_eventually_succeeds_on_different_citizen() -> None:
    """Fail once, succeed on second try — must end status='ok' with 2 attempts."""
    p = _plan(("research", []))
    nation = _FakeNation(
        outputs={"research": "the answer"},
        scripts={"research": [0.0, 1.0]},  # fail, then succeed
    )
    outcomes = await execute_plan(p, nation)  # type: ignore[arg-type]

    assert outcomes[0].status == "ok"
    assert len(outcomes[0].attempts) == 2
    assert outcomes[0].attempts[0].agent_id != outcomes[0].attempts[1].agent_id


@pytest.mark.asyncio
async def test_retry_forbids_previously_failed_citizen() -> None:
    """The second attempt must forbid the first attempt's citizen."""
    p = _plan(("research", []))
    nation = _FakeNation(scripts={"research": [0.0, 1.0]})
    await execute_plan(p, nation)  # type: ignore[arg-type]

    # First call has empty forbid; second call must include the first citizen.
    assert nation.calls[0][2] == frozenset()
    assert "ant-1" in nation.calls[1][2]


@pytest.mark.asyncio
async def test_retry_exhausted_marks_failed() -> None:
    """All max_attempts fail -> outcome.status == 'failed'."""
    p = _plan(("research", []))
    nation = _FakeNation(scripts={"research": [0.0, 0.0, 0.0, 0.0, 0.0]})
    outcomes = await execute_plan(p, nation, retry=RetryPolicy(max_attempts=3))  # type: ignore[arg-type]

    assert outcomes[0].status == "failed"
    assert len(outcomes[0].attempts) == 3


@pytest.mark.asyncio
async def test_downstream_skipped_when_dependency_fails() -> None:
    """research fails -> compare and recommend get status='skipped'."""
    p = _plan(("research", []), ("compare", ["research"]), ("recommend", ["compare"]))
    nation = _FakeNation(scripts={"research": [0.0, 0.0, 0.0]})

    outcomes = await execute_plan(p, nation, retry=RetryPolicy(max_attempts=3))  # type: ignore[arg-type]

    assert outcomes[0].status == "failed"
    assert outcomes[1].status == "skipped"
    assert outcomes[2].status == "skipped"
    assert "dependency 'research' failed" in (outcomes[1].skip_reason or "")


@pytest.mark.asyncio
async def test_independent_subtasks_unaffected_by_one_failure() -> None:
    """Two parallel independent subtasks — one fails, the other still runs."""
    p = _plan(("a", []), ("b", []))
    nation = _FakeNation(
        outputs={"b": "b-ok"},
        scripts={"a": [0.0, 0.0, 0.0]},
    )
    outcomes = await execute_plan(p, nation, retry=RetryPolicy(max_attempts=3))  # type: ignore[arg-type]

    assert outcomes[0].status == "failed"
    assert outcomes[1].status == "ok"


@pytest.mark.asyncio
async def test_no_retry_when_max_attempts_is_one() -> None:
    p = _plan(("research", []))
    nation = _FakeNation(scripts={"research": [0.0]})
    outcomes = await execute_plan(p, nation, retry=RetryPolicy(max_attempts=1))  # type: ignore[arg-type]
    assert len(outcomes[0].attempts) == 1
    assert outcomes[0].status == "failed"
