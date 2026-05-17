"""Executor tests — topological sort, retries, skip-on-failure, context.

We avoid live LLM calls. For execute_plan, we stub the nation's run method.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    async def run(self, task_type: str, prompt: str, *, forbid=None, on_token=None, **_kw):  # noqa: ANN201
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


# Parallel execution tests
import asyncio  # noqa: E402

from anthill.core.executor import _waves_from_topological_order  # noqa: E402


def test_waves_groups_independent_subtasks_together() -> None:
    """Two roots in wave 0; their fan-in child in wave 1."""
    p = _plan(("a", []), ("b", []), ("c", ["a"]))
    order = [0, 1, 2]
    waves = _waves_from_topological_order(p, order)
    assert waves[0] == [0, 1]
    assert waves[1] == [2]


def test_waves_chain_is_one_per_level() -> None:
    p = _plan(("a", []), ("b", ["a"]), ("c", ["b"]))
    order = [0, 1, 2]
    waves = _waves_from_topological_order(p, order)
    assert waves == [[0], [1], [2]]


class _SlowFakeNation:
    """Fake nation where each task takes a fixed real-time delay."""

    def __init__(self, delay: float = 0.1) -> None:
        self._delay = delay
        self.calls: list[tuple[str, float]] = []  # (task_type, start time)

    async def run(self, task_type: str, prompt: str, *, forbid=None, on_token=None, **_kw):  # noqa: ANN201
        import time as _time
        self.calls.append((task_type, _time.perf_counter()))
        await asyncio.sleep(self._delay)
        return _FakeResult(output="ok", success_score=1.0, task_type=task_type)


@pytest.mark.asyncio
async def test_independent_subtasks_run_in_parallel_by_default() -> None:
    """Three independent subtasks should finish in ~one delay, not three."""
    p = _plan(("a", []), ("b", []), ("c", []))
    nation = _SlowFakeNation(delay=0.1)
    import time as _time
    start = _time.perf_counter()
    await execute_plan(p, nation)  # type: ignore[arg-type]
    elapsed = _time.perf_counter() - start
    # Sequential would be ~0.3s; parallel should be < 0.2s.
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_parallel_can_be_disabled() -> None:
    p = _plan(("a", []), ("b", []), ("c", []))
    nation = _SlowFakeNation(delay=0.1)
    import time as _time
    start = _time.perf_counter()
    await execute_plan(p, nation, retry=RetryPolicy(parallel=False))  # type: ignore[arg-type]
    elapsed = _time.perf_counter() - start
    assert elapsed >= 0.25  # ~0.3 expected; allow small slack


@pytest.mark.asyncio
async def test_parallel_still_respects_dependencies() -> None:
    """A chain a -> b -> c cannot be parallelised, must take ~3 delays."""
    p = _plan(("a", []), ("b", ["a"]), ("c", ["b"]))
    nation = _SlowFakeNation(delay=0.1)
    import time as _time
    start = _time.perf_counter()
    await execute_plan(p, nation)  # type: ignore[arg-type]
    elapsed = _time.perf_counter() - start
    assert elapsed >= 0.25


# --- Progress callback tests ---------------------------------------------

from anthill.core.executor import ProgressEvent  # noqa: E402


@pytest.mark.asyncio
async def test_progress_callback_fires_started_and_finished() -> None:
    p = _plan(("a", []))
    nation = _FakeNation({"a": "ok"})
    events: list[ProgressEvent] = []

    async def collect(ev: ProgressEvent) -> None:
        events.append(ev)

    await execute_plan(p, nation, on_progress=collect)  # type: ignore[arg-type]
    kinds = [e.kind for e in events]
    assert "started" in kinds
    assert "attempt" in kinds
    assert "finished" in kinds
    # 'finished' is always the LAST event for a given subtask index.
    assert events[-1].kind == "finished"


@pytest.mark.asyncio
async def test_progress_callback_marks_outcome_durations() -> None:
    p = _plan(("a", []))
    nation = _FakeNation({"a": "ok"})
    events: list[ProgressEvent] = []

    async def collect(ev: ProgressEvent) -> None:
        events.append(ev)

    outcomes = await execute_plan(p, nation, on_progress=collect)  # type: ignore[arg-type]
    assert outcomes[0].started_at is not None
    assert outcomes[0].ended_at is not None
    assert outcomes[0].duration_seconds >= 0.0


@pytest.mark.asyncio
async def test_progress_attempt_event_marks_success_flag() -> None:
    p = _plan(("a", []))
    nation = _FakeNation(scripts={"a": [0.0, 1.0]})  # fail, then succeed
    events: list[ProgressEvent] = []

    async def collect(ev: ProgressEvent) -> None:
        events.append(ev)

    await execute_plan(p, nation, on_progress=collect)  # type: ignore[arg-type]
    attempts = [e for e in events if e.kind == "attempt"]
    assert len(attempts) == 2
    assert attempts[0].success is False
    assert attempts[1].success is True


@pytest.mark.asyncio
async def test_progress_callback_for_skipped_subtask() -> None:
    """A subtask skipped due to dep failure should emit only 'finished'."""
    p = _plan(("a", []), ("b", ["a"]))
    nation = _FakeNation(scripts={"a": [0.0, 0.0, 0.0]})  # always fails
    events: list[ProgressEvent] = []

    async def collect(ev: ProgressEvent) -> None:
        events.append(ev)

    await execute_plan(p, nation, on_progress=collect, retry=RetryPolicy(max_attempts=3))  # type: ignore[arg-type]
    b_events = [e for e in events if e.subtask.task_type == "b"]
    # b should only have 'finished' (skipped), no 'started' / 'attempt'.
    assert all(e.kind == "finished" for e in b_events)
    assert b_events[0].outcome.status == "skipped"
