"""0.2.14 — `/retry` re-asks the last question with a DIFFERENT citizen.

The point of `/retry` in a multi-model nation isn't "give me the same
thing again" — it's "let another model try, and let pheromone learn
which one was better." Implementation:

  - `/retry` reads the persisted last ask record
  - Builds a forbid set from `last.pairs` (every agent_id that ran it)
  - Queues the request via stats.queued_retry_{request,forbid}
  - The REPL main loop intercepts on the next iteration, threads
    forbid down through _handle_ask → nation.ask → execute_plan →
    _run_one_subtask where it seeds the per-subtask forbid set

Tests cover the executor-level plumbing (initial_forbid seeds the
ban list) and the stats queue contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anthill.cli.repl import SessionStats
from anthill.core.agent import Agent
from anthill.core.executor import execute_plan
from anthill.core.feedback import AskRecord, load_last_ask, save_last_ask
from anthill.core.nation import Nation
from anthill.core.scout import Plan, Subtask


# --- queued-retry stats contract ----------------------------------------


def test_session_stats_starts_with_no_queued_retry() -> None:
    stats = SessionStats()
    assert stats.queued_retry_request is None
    assert stats.queued_retry_forbid is None


def test_session_stats_can_carry_queued_retry() -> None:
    stats = SessionStats()
    stats.queued_retry_request = "回顾一下 mysql 部署"
    stats.queued_retry_forbid = {"ant-1", "ant-2"}
    assert stats.queued_retry_request == "回顾一下 mysql 部署"
    assert stats.queued_retry_forbid == {"ant-1", "ant-2"}


# --- executor honors initial_forbid -------------------------------------


def _make_nation_with_two_agents() -> Nation:
    """Two agents able to do 'research'. The router picks whichever
    has stronger pheromones (or random for tied cold-starts)."""
    n = Nation(name="t")
    n.agents = [
        Agent(id="ant-A", model="deepseek"),
        Agent(id="ant-B", model="minimax"),
    ]
    # Seed strong pheromones for ant-A so without forbid it'd win.
    for _ in range(8):
        n.pheromones.deposit("ant-A", "research", success_score=1.0)
    return n


class _FakeAgent(Agent):
    """A pass-through agent that just records which agent_id ran it."""

    async def execute(self, task_type, prompt, *, system=None, on_token=None, **kwargs):  # type: ignore[override]
        import uuid

        from anthill.core.agent import TaskResult
        return TaskResult(
            task_id=f"task-{uuid.uuid4().hex[:8]}",
            agent_id=self.id,
            task_type=task_type,
            output=f"[{self.id} did {task_type}]",
            success_score=1.0,
            duration_seconds=0.01,
            input_tokens=10,
            output_tokens=10,
        )


def test_execute_plan_with_initial_forbid_skips_banned_agent() -> None:
    """Seed strong pheromone on ant-A so the router prefers it. Then
    pass initial_forbid={ant-A}. Result: ant-B runs instead."""
    n = Nation(name="t")
    n.agents = [
        _FakeAgent(id="ant-A", model="deepseek"),
        _FakeAgent(id="ant-B", model="minimax"),
    ]
    # Strong trail on ant-A.
    for _ in range(8):
        n.pheromones.deposit("ant-A", "research", success_score=1.0)
    plan = Plan(subtasks=[Subtask("research", "do x", [])])

    outcomes = asyncio.run(
        execute_plan(plan, n, initial_forbid={"ant-A"})
    )

    assert len(outcomes) == 1
    final = outcomes[0].final
    assert final is not None
    # The forbidden agent didn't run.
    assert final.agent_id == "ant-B"


def test_execute_plan_without_initial_forbid_picks_strongest() -> None:
    """Sanity: with no forbid, the strong-trail agent wins."""
    n = Nation(name="t")
    n.agents = [
        _FakeAgent(id="ant-A", model="deepseek"),
        _FakeAgent(id="ant-B", model="minimax"),
    ]
    for _ in range(8):
        n.pheromones.deposit("ant-A", "research", success_score=1.0)
    plan = Plan(subtasks=[Subtask("research", "do x", [])])

    outcomes = asyncio.run(execute_plan(plan, n))

    assert outcomes[0].final is not None
    assert outcomes[0].final.agent_id == "ant-A"


def test_execute_plan_initial_forbid_does_not_mutate_caller_set() -> None:
    """The executor accumulates more agents into the forbid set on
    failure. If we mutate the CALLER's set the next /retry would
    inherit stale bans."""
    n = Nation(name="t")
    n.agents = [
        _FakeAgent(id="ant-A", model="deepseek"),
        _FakeAgent(id="ant-B", model="minimax"),
    ]
    for _ in range(5):
        n.pheromones.deposit("ant-A", "research", success_score=1.0)
    plan = Plan(subtasks=[Subtask("research", "x", [])])
    caller_forbid = {"ant-A"}
    asyncio.run(execute_plan(plan, n, initial_forbid=caller_forbid))
    # Still just the one we passed in. Executor's internal copy
    # doesn't leak back.
    assert caller_forbid == {"ant-A"}


# --- nation.ask integration --------------------------------------------


def test_nation_ask_passes_forbid_through(tmp_path: Path) -> None:
    """nation.ask(..., forbid={'ant-A'}) → ant-B does the work even
    though ant-A has a stronger trail."""
    n = Nation(name="t")
    n.agents = [
        _FakeAgent(id="ant-A", model="deepseek"),
        _FakeAgent(id="ant-B", model="minimax"),
    ]
    for _ in range(8):
        n.pheromones.deposit("ant-A", "research", success_score=1.0)

    # Pre-plan path so we skip Scout (no LLM in this test).
    plan = Plan(subtasks=[Subtask("research", "do it", [])])
    result = asyncio.run(
        n.ask(
            "research request",
            pre_plan=plan,
            forbid={"ant-A"},
            nation_dir=tmp_path,
        )
    )

    assert len(result.outcomes) == 1
    assert result.outcomes[0].final is not None
    assert result.outcomes[0].final.agent_id == "ant-B"


# --- /retry builds forbid from last ask's pairs ------------------------


def test_retry_forbid_set_from_ask_pairs(tmp_path: Path) -> None:
    """The /retry handler logic: forbid every agent_id from the
    last ask's pairs."""
    rec = AskRecord(
        request="mysql 部署怎么搞",
        timestamp=100.0,
        pairs=[("ant-A", "research"), ("ant-B", "summarize")],
    )
    save_last_ask(rec, tmp_path)
    loaded = load_last_ask(tmp_path)
    assert loaded is not None

    # The same expression /retry uses:
    forbid = {aid for aid, _tt in loaded.pairs}
    assert forbid == {"ant-A", "ant-B"}


def test_retry_no_prior_ask_returns_none(tmp_path: Path) -> None:
    """Calling /retry with no prior ask → load_last_ask returns
    None → the handler prints a 'nothing to retry' message and
    doesn't queue anything. We assert the None contract."""
    assert load_last_ask(tmp_path) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
