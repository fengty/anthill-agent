"""Self-correction loop tests — replan around a failed subtask.

The Scout integration uses live LLM normally, but here we stub out
`Scout.replan` so we exercise the orchestration without making API
calls. The two layers we care about:

1. Scout.replan strict parsing — it must return None on bad output
   so the Nation falls back to partial results instead of fabricating
   a fake recovery plan.
2. Nation.ask actually splices, re-executes, and surfaces a replan
   count — with the kept OK steps carried through as resume_state.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from anthill.core.agent import Agent
from anthill.core.budget import Budget
from anthill.core.nation import Nation
from anthill.core.plan_cache import remember as cache_remember
from anthill.core.scout import Plan, Scout, Subtask


# --- shared fakes ----------------------------------------------------------


@dataclass
class _FakeResult:
    output: str
    success_score: float = 1.0
    agent_id: str = "ant-1"
    task_type: str = ""
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    task_id: str = "task-fake"


def _make_nation_with_scripts(scripts: dict[str, list[float]]) -> Nation:
    """Build a Nation whose run() follows a per-task-type score script."""
    n = Nation(name="testnat")
    n.agents = [Agent(model="deepseek-chat", id=f"ant-{i}") for i in range(1, 4)]
    counters: dict[str, int] = {}

    async def fake_run(task_type: str, prompt: str, *, forbid=None):  # noqa: ANN201
        idx = counters.get(task_type, 0)
        counters[task_type] = idx + 1
        avail = [a for a in n.agents if a.id not in (forbid or set())]
        agent_id = avail[0].id if avail else "ant-1"
        script = scripts.get(task_type, [])
        score = script[idx] if idx < len(script) else 1.0
        return _FakeResult(
            output=f"<{task_type}>" if score > 0 else "[error]",
            success_score=score,
            agent_id=agent_id,
            task_type=task_type,
        )

    n.run = fake_run  # type: ignore[assignment]
    return n


# --- Scout.replan parser ---------------------------------------------------


@pytest.mark.asyncio
async def test_replan_returns_none_on_unparseable_response() -> None:
    """Bad/empty output must NOT degrade to a fake 'general' plan.

    The fallback that powers Scout._parse is right for fresh planning
    (better one weak task than nothing). For replan it would silently
    erase the user's existing partial outputs in favor of a one-shot
    redo. Strict only.
    """
    scout = Scout(model="deepseek-chat")

    class _BadResponse:
        text = "Sorry, I cannot help with that."

    class _BadProvider:
        async def complete(self, *args, **kwargs):  # noqa: ANN001, ARG002
            return _BadResponse()

    with patch("anthill.core.scout.get_provider", return_value=_BadProvider()):
        out = await scout.replan(
            "original request",
            succeeded=[],
            failed=Subtask(task_type="x", prompt="do x", depends_on=[]),
            failure_reason="all citizens errored",
            remaining=[],
        )
    assert out is None


@pytest.mark.asyncio
async def test_replan_parses_valid_json() -> None:
    scout = Scout(model="deepseek-chat")

    class _GoodResponse:
        text = (
            '{"plan": ['
            '{"task_type": "research_v2", "prompt": "try harder", "depends_on": []}'
            "]}"
        )

    class _GoodProvider:
        async def complete(self, *args, **kwargs):  # noqa: ANN001, ARG002
            return _GoodResponse()

    with patch("anthill.core.scout.get_provider", return_value=_GoodProvider()):
        plan = await scout.replan(
            "original",
            succeeded=[],
            failed=Subtask(task_type="research", prompt="dig", depends_on=[]),
            failure_reason="empty",
            remaining=[],
        )
    assert plan is not None
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].task_type == "research_v2"


# --- Nation.ask integration -----------------------------------------------


@pytest.mark.asyncio
async def test_failing_subtask_triggers_replan_and_recovers() -> None:
    """Original plan fails on step 0; salvage plan succeeds — replans=1."""
    n = _make_nation_with_scripts(
        scripts={
            # Original 'research' fails on every available citizen (3 tries).
            "research": [0.0, 0.0, 0.0],
            # Replacement 'research_v2' succeeds first try.
            "research_v2": [1.0],
        }
    )
    plan = Plan(
        subtasks=[
            Subtask(task_type="research", prompt="dig", depends_on=[]),
            Subtask(task_type="summarize", prompt="sum", depends_on=["research"]),
        ]
    )
    cache_remember("two-step", plan, n.plan_cache)

    salvage_plan = Plan(
        subtasks=[
            Subtask(task_type="research_v2", prompt="try differently", depends_on=[]),
            Subtask(task_type="summarize", prompt="sum", depends_on=["research_v2"]),
        ]
    )

    async def fake_replan(self, request, **kwargs):  # noqa: ANN001, ARG001
        return salvage_plan

    with patch.object(Scout, "replan", new=fake_replan):
        result = await n.ask("two-step", max_replans=1)

    assert result.replans == 1
    # Final plan reflects the salvage shape.
    assert [s.task_type for s in result.plan.subtasks] == ["research_v2", "summarize"]
    assert all(o.status == "ok" for o in result.outcomes)


@pytest.mark.asyncio
async def test_max_replans_zero_disables_self_correction() -> None:
    """When the user opts out, a failure stays a failure."""
    n = _make_nation_with_scripts(scripts={"research": [0.0, 0.0, 0.0]})
    plan = Plan(
        subtasks=[Subtask(task_type="research", prompt="dig", depends_on=[])]
    )
    cache_remember("solo", plan, n.plan_cache)

    replan_called = False

    async def fake_replan(self, request, **kwargs):  # noqa: ANN001, ARG001
        nonlocal replan_called
        replan_called = True
        return Plan(
            subtasks=[Subtask(task_type="x", prompt="x", depends_on=[])]
        )

    with patch.object(Scout, "replan", new=fake_replan):
        result = await n.ask("solo", max_replans=0)

    assert result.replans == 0
    assert replan_called is False
    assert result.outcomes[0].status == "failed"


@pytest.mark.asyncio
async def test_replan_skipped_when_scout_returns_none() -> None:
    """A bad/empty replan must not erase the original partial result."""
    n = _make_nation_with_scripts(scripts={"research": [0.0, 0.0, 0.0]})
    plan = Plan(
        subtasks=[Subtask(task_type="research", prompt="dig", depends_on=[])]
    )
    cache_remember("solo", plan, n.plan_cache)

    async def fake_replan(self, request, **kwargs):  # noqa: ANN001, ARG001
        return None

    with patch.object(Scout, "replan", new=fake_replan):
        result = await n.ask("solo", max_replans=2)

    assert result.replans == 0
    assert result.outcomes[0].status == "failed"
    # Original plan preserved.
    assert result.plan.subtasks[0].task_type == "research"


@pytest.mark.asyncio
async def test_replan_preserves_already_succeeded_steps() -> None:
    """OK step before the failure must carry through as resume state."""
    n = _make_nation_with_scripts(
        scripts={
            "extract": [1.0],                  # succeeds first try
            "translate": [0.0, 0.0, 0.0],      # fails all retries
            "translate_v2": [1.0],             # salvage succeeds
        }
    )
    call_log: list[str] = []
    original_run = n.run  # type: ignore[has-type]

    async def logged_run(task_type, prompt, *, forbid=None):  # noqa: ANN001, ANN201
        call_log.append(task_type)
        return await original_run(task_type, prompt, forbid=forbid)

    n.run = logged_run  # type: ignore[assignment]

    plan = Plan(
        subtasks=[
            Subtask(task_type="extract", prompt="extract text", depends_on=[]),
            Subtask(task_type="translate", prompt="translate", depends_on=["extract"]),
        ]
    )
    cache_remember("doc", plan, n.plan_cache)

    salvage = Plan(
        subtasks=[
            Subtask(task_type="translate_v2", prompt="translate (gentler)",
                    depends_on=["extract"]),
        ]
    )

    async def fake_replan(self, request, **kwargs):  # noqa: ANN001, ARG001
        return salvage

    with patch.object(Scout, "replan", new=fake_replan):
        result = await n.ask("doc", max_replans=1)

    assert result.replans == 1
    # 'extract' must not have been re-run after the replan — that was the
    # whole point of the resume_state path.
    assert call_log.count("extract") == 1
    # The final plan = extract + translate_v2; both must be OK.
    assert [s.task_type for s in result.plan.subtasks] == ["extract", "translate_v2"]
    assert all(o.status == "ok" for o in result.outcomes)


@pytest.mark.asyncio
async def test_replan_does_not_run_when_budget_exhausted() -> None:
    """If a cap blew, we already know spending more won't help."""
    n = _make_nation_with_scripts(scripts={"research": [0.0]})

    async def fake_run(task_type, prompt, *, forbid=None):  # noqa: ANN001, ANN201
        return _FakeResult(
            output="[error]" if task_type == "research" else "ok",
            success_score=0.0 if task_type == "research" else 1.0,
            agent_id="ant-1",
            task_type=task_type,
            input_tokens=2000,
            output_tokens=2000,
        )

    n.run = fake_run  # type: ignore[assignment]
    plan = Plan(
        subtasks=[Subtask(task_type="research", prompt="dig", depends_on=[])]
    )
    cache_remember("solo", plan, n.plan_cache)

    replan_called = False

    async def fake_replan(self, request, **kwargs):  # noqa: ANN001, ARG001
        nonlocal replan_called
        replan_called = True
        return None

    with patch.object(Scout, "replan", new=fake_replan):
        result = await n.ask(
            "solo",
            max_replans=1,
            budget=Budget(max_tokens=1000),
        )

    assert result.budget is not None
    assert result.budget.exhausted is not None
    assert replan_called is False  # budget gate blocked the replan attempt
    assert result.replans == 0
