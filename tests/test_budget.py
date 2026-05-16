"""Budget tests — caps actually stop work; partial output is still returned.

Three layers covered:
- Budget/BudgetTracker math: tokens, cost, time accounting.
- execute_plan honors the tracker: pre-flight check, mid-retry check,
  remaining subtasks marked 'skipped' with a reason.
- Nation.ask wires Budget through and surfaces the snapshot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from anthill.core.budget import (
    Budget,
    BudgetTracker,
    reason_label,
    snapshot,
)
from anthill.core.executor import execute_plan
from anthill.core.scout import Plan, Subtask


# --- helpers ---------------------------------------------------------------


def _plan(*specs: tuple[str, list[str]]) -> Plan:
    return Plan(
        subtasks=[
            Subtask(task_type=tt, prompt=f"do {tt}", depends_on=list(deps))
            for tt, deps in specs
        ]
    )


@dataclass
class _FakeResult:
    output: str
    success_score: float = 1.0
    agent_id: str = "ant-fake"
    task_type: str = ""
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    task_id: str = "task-fake"


class _FakeNation:
    def __init__(
        self,
        outputs: dict[str, str] | None = None,
        token_usage: dict[str, tuple[int, int]] | None = None,
        agents: list[str] | None = None,
        scripts: dict[str, list[float]] | None = None,
    ) -> None:
        self._outputs = outputs or {}
        self._agents = agents or ["ant-1", "ant-2", "ant-3"]
        self._tokens = token_usage or {}
        self._scripts = scripts or {}
        self._counters: dict[str, int] = {}
        self.calls: list[tuple[str, frozenset[str]]] = []

    async def run(self, task_type: str, prompt: str, *, forbid=None):  # noqa: ANN201
        forbid_set = frozenset(forbid or set())
        self.calls.append((task_type, forbid_set))
        available = [a for a in self._agents if a not in forbid_set]
        agent_id = available[0]
        attempt_idx = self._counters.get(task_type, 0)
        self._counters[task_type] = attempt_idx + 1
        script = self._scripts.get(task_type, [])
        score = script[attempt_idx] if attempt_idx < len(script) else 1.0
        in_t, out_t = self._tokens.get(task_type, (100, 100))
        return _FakeResult(
            output=self._outputs.get(task_type, f"<{task_type}>"),
            success_score=score,
            agent_id=agent_id,
            task_type=task_type,
            input_tokens=in_t,
            output_tokens=out_t,
        )


def _lookup_deepseek(_agent_id: str) -> str:
    """Force a known price ($0.27 in / $1.10 out per million for deepseek-chat)."""
    return "deepseek-chat"


# --- Budget primitives -----------------------------------------------------


def test_empty_budget_is_a_noop() -> None:
    assert Budget().is_empty() is True
    assert Budget(max_tokens=1).is_empty() is False
    assert Budget(max_cost_usd=0.0).is_empty() is False
    assert Budget(max_seconds=0.0).is_empty() is False


def test_tracker_token_accounting() -> None:
    t = BudgetTracker(Budget(max_tokens=1000), model_lookup=_lookup_deepseek)
    assert t.may_run_next() is None
    t.record_attempt("ant-1", 300, 200)
    assert t.spent_tokens == 500
    assert t.may_run_next() is None
    t.record_attempt("ant-1", 400, 200)  # cumulative 1100 > cap 1000
    assert t.may_run_next() == "tokens"


def test_tracker_cost_accounting_uses_real_prices() -> None:
    """deepseek-chat is $0.27 in / $1.10 out per million tokens."""
    t = BudgetTracker(Budget(max_cost_usd=0.001), model_lookup=_lookup_deepseek)
    # 1000 input + 1000 output = 0.00027 + 0.00110 = 0.00137 → over $0.001.
    t.record_attempt("ant-1", 1000, 1000)
    assert t.spent_usd == pytest.approx(0.00137, abs=1e-6)
    assert t.may_run_next() == "cost"


def test_tracker_time_check() -> None:
    t = BudgetTracker(Budget(max_seconds=0.05), model_lookup=_lookup_deepseek)
    assert t.may_run_next() is None
    time.sleep(0.06)
    assert t.may_run_next() == "time"


def test_reason_label_is_human_readable() -> None:
    assert "token" in reason_label("tokens")
    assert "cost" in reason_label("cost")
    assert "time" in reason_label("time")


def test_snapshot_captures_running_state() -> None:
    t = BudgetTracker(Budget(max_tokens=10_000), model_lookup=_lookup_deepseek)
    t.record_attempt("x", 500, 500)
    snap = snapshot(t)
    assert snap.tokens == 1000
    assert snap.exhausted is None
    assert "1,000" in snap.summary or "1000" in snap.summary


def test_remaining_summary_includes_caps_when_set() -> None:
    t = BudgetTracker(
        Budget(max_tokens=10_000, max_cost_usd=1.0, max_seconds=60.0),
        model_lookup=_lookup_deepseek,
    )
    t.record_attempt("x", 100, 100)
    summary = t.remaining_summary()
    assert "/ $1.00" in summary or "/ $1.0000" in summary
    assert "/ 10,000 tokens" in summary
    assert "/ 60s" in summary


# --- Executor integration --------------------------------------------------


@pytest.mark.asyncio
async def test_executor_skips_remaining_when_token_cap_blown() -> None:
    """First subtask burns the budget; remaining ones should be skipped."""
    p = _plan(("research", []), ("draft", ["research"]))
    nation = _FakeNation(token_usage={"research": (600, 600)})
    tracker = BudgetTracker(Budget(max_tokens=1000), model_lookup=_lookup_deepseek)

    outcomes = await execute_plan(p, nation, budget=tracker)  # type: ignore[arg-type]

    assert outcomes[0].status == "ok"
    assert outcomes[1].status == "skipped"
    assert outcomes[1].skip_reason is not None
    assert "token" in outcomes[1].skip_reason
    # The draft subtask should not have been called.
    assert all(tt != "draft" for tt, _ in nation.calls)


@pytest.mark.asyncio
async def test_executor_stops_retrying_within_subtask_on_budget() -> None:
    """If retries would push us past the budget, stop after the failing attempt."""
    p = _plan(("research", []))
    # First attempt fails (score=0) but still costs tokens; budget blown after it.
    nation = _FakeNation(
        scripts={"research": [0.0, 1.0, 1.0]},
        token_usage={"research": (600, 600)},
    )
    tracker = BudgetTracker(Budget(max_tokens=1000), model_lookup=_lookup_deepseek)

    outcomes = await execute_plan(p, nation, budget=tracker)  # type: ignore[arg-type]

    # Only one attempt happened — retry was suppressed by the cap.
    assert len(outcomes[0].attempts) == 1
    assert outcomes[0].status == "failed"


@pytest.mark.asyncio
async def test_executor_runs_normally_when_under_budget() -> None:
    p = _plan(("a", []), ("b", ["a"]))
    nation = _FakeNation(token_usage={"a": (50, 50), "b": (50, 50)})
    tracker = BudgetTracker(Budget(max_tokens=10_000), model_lookup=_lookup_deepseek)

    outcomes = await execute_plan(p, nation, budget=tracker)  # type: ignore[arg-type]

    assert all(o.status == "ok" for o in outcomes)
    assert tracker.spent_tokens == 200


@pytest.mark.asyncio
async def test_executor_works_without_budget() -> None:
    """budget=None must remain a no-op for backwards compat."""
    p = _plan(("solo", []))
    nation = _FakeNation()
    outcomes = await execute_plan(p, nation)  # type: ignore[arg-type]
    assert outcomes[0].status == "ok"


# --- Nation.ask wiring -----------------------------------------------------


@pytest.mark.asyncio
async def test_nation_ask_surfaces_budget_snapshot_in_result() -> None:
    """AskResult.budget is filled when a Budget was provided."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.plan_cache import remember as cache_remember

    n = Nation(name="testnat")
    n.agents = [Agent(model="deepseek-chat", id="ant-1")]

    async def fake_run(task_type: str, prompt: str, *, forbid=None):  # noqa: ANN201
        return _FakeResult(
            output=f"<{task_type}>",
            agent_id="ant-1",
            task_type=task_type,
            input_tokens=100,
            output_tokens=100,
        )

    n.run = fake_run  # type: ignore[assignment]
    plan = _plan(("x", []))
    cache_remember("hi", plan, n.plan_cache)

    result = await n.ask("hi", budget=Budget(max_tokens=10_000))
    assert result.budget is not None
    assert result.budget.tokens == 200
    assert result.budget.exhausted is None


@pytest.mark.asyncio
async def test_nation_ask_budget_blank_stays_unset() -> None:
    """An all-None Budget should not allocate a tracker (and no snapshot)."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.plan_cache import remember as cache_remember

    n = Nation(name="testnat")
    n.agents = [Agent(model="deepseek-chat", id="ant-1")]

    async def fake_run(task_type: str, prompt: str, *, forbid=None):  # noqa: ANN201
        return _FakeResult(output="ok", agent_id="ant-1", task_type=task_type)

    n.run = fake_run  # type: ignore[assignment]
    plan = _plan(("x", []))
    cache_remember("hi", plan, n.plan_cache)

    # Both flavors: no budget kwarg, and an empty one.
    r1 = await n.ask("hi")
    r2 = await n.ask("hi", budget=Budget())
    assert r1.budget is None
    assert r2.budget is None


@pytest.mark.asyncio
async def test_nation_ask_exhausted_budget_propagates_to_snapshot() -> None:
    """When the cap blows mid-ask, snapshot.exhausted names the reason."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.plan_cache import remember as cache_remember

    n = Nation(name="testnat")
    n.agents = [Agent(model="deepseek-chat", id="ant-1")]

    async def fake_run(task_type: str, prompt: str, *, forbid=None):  # noqa: ANN201
        return _FakeResult(
            output=f"<{task_type}>",
            agent_id="ant-1",
            task_type=task_type,
            input_tokens=600,
            output_tokens=600,
        )

    n.run = fake_run  # type: ignore[assignment]
    plan = _plan(("a", []), ("b", ["a"]))
    cache_remember("two-step", plan, n.plan_cache)

    result = await n.ask("two-step", budget=Budget(max_tokens=1000))
    assert result.budget is not None
    assert result.budget.exhausted == "tokens"
    assert result.outcomes[0].status == "ok"
    assert result.outcomes[1].status == "skipped"
