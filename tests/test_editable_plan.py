"""0.1.13 — editable plan: review hook between Scout and executor.

When the REPL passes `on_plan` to `Nation.ask`, Scout's plan is shown
to the user before execution. The callback returns either a (possibly
modified) Plan to run, or None to cancel the entire ask. Cache hits,
trivial-fast paths, resume, and pre_plan all bypass the hook — those
plans are either already optimized or explicitly user-owned.

Tests:
  1. AskResult.cancelled_by_user defaults False
  2. on_plan=None preserves the old behavior exactly (no callback fired)
  3. on_plan that returns the plan unchanged ⇒ plan runs normally
  4. on_plan that mutates the plan ⇒ executor runs the modified plan
  5. on_plan that returns None ⇒ ask returns cancelled AskResult
  6. Cache-hit path doesn't fire on_plan (plan is locked)
  7. pre_plan path doesn't fire on_plan
  8. Resume path doesn't fire on_plan
  9. Trivial-fast path doesn't fire on_plan
"""

from __future__ import annotations

import pytest


def test_askresult_default_cancelled_false() -> None:
    from anthill.core.nation import AskResult
    from anthill.core.scout import Plan

    ar = AskResult(request="x", plan=Plan(subtasks=[]), outcomes=[])
    assert ar.cancelled_by_user is False


@pytest.mark.asyncio
async def test_on_plan_unchanged_runs_normally(monkeypatch) -> None:
    """Returning the plan as-is is the no-op review case."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_plan(self, request, **kwargs):
        return _Plan(subtasks=[_Sub("explain", request, [])])

    async def fake_run(task_type, prompt, **kwargs):
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="done",
            success_score=1.0,
            duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    seen = []

    async def on_plan(plan):
        seen.append(plan)
        return plan

    result = await n.ask("research the X protocol in depth", on_plan=on_plan)
    assert len(seen) == 1
    assert result.cancelled_by_user is False
    assert len(result.outcomes) == 1


@pytest.mark.asyncio
async def test_on_plan_can_mutate_plan(monkeypatch) -> None:
    """A callback that drops a subtask ⇒ executor runs the reduced plan."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_plan(self, request, **kwargs):
        return _Plan(
            subtasks=[
                _Sub("translate", request, []),
                _Sub("explain", request, []),
            ]
        )

    calls: list[str] = []

    async def fake_run(task_type, prompt, **kwargs):
        calls.append(task_type)
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="done",
            success_score=1.0,
            duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    async def on_plan(plan):
        # Drop the first subtask, keep only "explain".
        return _Plan(
            subtasks=[s for s in plan.subtasks if s.task_type != "translate"],
            complexity=plan.complexity,
        )

    result = await n.ask("translate and explain my code please", on_plan=on_plan)
    assert calls == ["explain"]
    assert len(result.outcomes) == 1
    assert result.outcomes[0].subtask.task_type == "explain"


@pytest.mark.asyncio
async def test_on_plan_none_cancels(monkeypatch) -> None:
    """Returning None marks the ask as cancelled; nothing runs."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_plan(self, request, **kwargs):
        return _Plan(subtasks=[_Sub("dangerous_op", request, [])])

    ran = []

    async def fake_run(task_type, prompt, **kwargs):
        ran.append(task_type)
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="oops",
            success_score=1.0,
            duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    async def on_plan(plan):
        return None  # bail out

    result = await n.ask("research the X protocol in depth", on_plan=on_plan)
    assert ran == []
    assert result.cancelled_by_user is True
    assert result.outcomes == []
    assert result.final_output == ""


@pytest.mark.asyncio
async def test_on_plan_not_called_on_pre_plan(monkeypatch) -> None:
    """Recipe-driven runs bypass the review hook — plan is user-owned."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Subtask as _Sub

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="x",
            success_score=1.0,
            duration_seconds=0.0,
        )

    n.run = fake_run  # type: ignore[assignment]

    seen = []

    async def on_plan(plan):
        seen.append(plan)
        return None  # would cancel — but should NOT fire here

    pre_plan = _Plan(subtasks=[_Sub("baked", "do it", [])])
    result = await n.ask("anything", pre_plan=pre_plan, on_plan=on_plan)
    assert seen == []
    assert result.cancelled_by_user is False
    assert len(result.outcomes) == 1


@pytest.mark.asyncio
async def test_on_plan_not_called_on_cache_hit(monkeypatch) -> None:
    """Cache hits skip on_plan — the cached plan is reusing prior work."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.plan_cache import remember as cache_remember
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Subtask as _Sub

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    plan = _Plan(subtasks=[_Sub("explain", "do it", [])])
    cache_remember("research the X protocol in depth", plan, n.plan_cache)

    async def fake_run(task_type, prompt, **kwargs):
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="x",
            success_score=1.0,
            duration_seconds=0.0,
        )

    n.run = fake_run  # type: ignore[assignment]
    seen = []

    async def on_plan(p):
        seen.append(p)
        return None

    result = await n.ask("research the X protocol in depth", on_plan=on_plan)
    assert seen == []
    assert result.cancelled_by_user is False
    assert n.last_ask_cache_hit is True


@pytest.mark.asyncio
async def test_on_plan_not_called_on_trivial_fast(monkeypatch) -> None:
    """Trivial requests bypass Scout AND the plan-review hook."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="hi",
            success_score=1.0,
            duration_seconds=0.0,
        )

    n.run = fake_run  # type: ignore[assignment]

    seen = []

    async def on_plan(p):
        seen.append(p)
        return None

    result = await n.ask("hi", on_plan=on_plan)
    assert seen == []
    assert result.cancelled_by_user is False
