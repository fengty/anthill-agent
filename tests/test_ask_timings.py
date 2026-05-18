"""0.1.44 — Nation.ask returns per-phase timing breakdown.

Why these tests: 0.1.43 left us with one number (`duration`) per turn.
Real-world asks have hit 45-103s with no way to tell whether Scout,
a slow subtask, or refusal-retry ate the time. AskTimings carries
the breakdown; this file verifies:

  1. Every ask path fills timings.total_seconds > 0
  2. plan_source labels each shortcut (trivial / cache / skill / scout)
  3. Scout time is captured only when Scout actually ran
  4. Per-subtask wall-clock is computed from outcome started_at/ended_at
  5. Refusal-retry count surfaces from attempt.failure_reason
  6. SessionTurn.to_dict carries the new timings field
     (and from_dict tolerates older logs that don't have it)
"""

from __future__ import annotations

import pytest

from anthill.core.agent import Agent, TaskResult
from anthill.core.nation import AskTimings, Nation
from anthill.core.scout import Plan as _Plan, Scout as _Scout, Subtask as _Sub
from anthill.core.sessions import SessionTurn


# --- Helpers ---------------------------------------------------------------


def _ok_result(task_type: str = "general", *, failure_reason: str | None = None):
    return TaskResult(
        task_id="t",
        agent_id="ant-1",
        task_type=task_type,
        output="done",
        success_score=1.0,
        duration_seconds=0.0,
        failure_reason=failure_reason,
    )


# --- AskTimings dataclass --------------------------------------------------


def test_asktimings_default_shape() -> None:
    t = AskTimings()
    assert t.total_seconds == 0.0
    assert t.scout_seconds is None
    assert t.subtask_seconds == []
    assert t.refusal_retry_count == 0
    assert t.plan_source == "scout"


def test_asktimings_to_dict_round_trips() -> None:
    t = AskTimings(
        total_seconds=14.829,
        scout_seconds=3.12,
        subtask_seconds=[("research", 6.4), ("analyze", 5.3)],
        refusal_retry_count=1,
        plan_source="scout",
    )
    d = t.to_dict()
    assert d["total_seconds"] == 14.829
    assert d["scout_seconds"] == 3.12
    assert d["subtask_seconds"] == [["research", 6.4], ["analyze", 5.3]]
    assert d["refusal_retry_count"] == 1
    assert d["plan_source"] == "scout"


def test_asktimings_scout_seconds_none_serialises_as_null() -> None:
    # cache / trivial / skill / pre_plan paths leave scout_seconds None;
    # we must keep it None in the dict so post-hoc analysis can tell
    # "Scout was bypassed" apart from "Scout ran but took 0s."
    t = AskTimings(plan_source="cache")
    assert t.to_dict()["scout_seconds"] is None


# --- Nation.ask integration ------------------------------------------------


@pytest.mark.asyncio
async def test_ask_trivial_fast_path_marks_source_and_skips_scout(
    monkeypatch,
) -> None:
    """`fast_classify` says trivial → no Scout call, plan_source="trivial"."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    scout_calls = 0

    async def fake_plan(self, *a, **kw):  # noqa: ANN001, ANN201, ARG002
        nonlocal scout_calls
        scout_calls += 1
        return _Plan(subtasks=[_Sub("general", "x", [])])

    monkeypatch.setattr(_Scout, "plan", fake_plan)

    result = await n.ask("hi")  # 2 chars → trivial
    assert scout_calls == 0, "Scout must not be called for trivial fast-path"
    assert result.timings.plan_source == "trivial"
    assert result.timings.scout_seconds is None
    assert result.timings.total_seconds > 0


@pytest.mark.asyncio
async def test_ask_normal_path_captures_scout_time(monkeypatch) -> None:
    """A non-trivial request runs Scout; scout_seconds must be populated."""
    import asyncio

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    async def slow_plan(self, *a, **kw):  # noqa: ANN001, ANN201, ARG002
        # Tiny but measurable delay so scout_seconds > 0.
        await asyncio.sleep(0.01)
        return _Plan(subtasks=[_Sub("general", "do it", [])])

    monkeypatch.setattr(_Scout, "plan", slow_plan)

    result = await n.ask(
        "help me figure out a presentation thing for next week"
    )
    assert result.timings.plan_source == "scout"
    assert result.timings.scout_seconds is not None
    assert result.timings.scout_seconds >= 0.005  # asyncio.sleep is approximate
    assert result.timings.total_seconds >= result.timings.scout_seconds


@pytest.mark.asyncio
async def test_ask_pre_plan_path_marks_pre_plan_source(monkeypatch) -> None:
    """Recipe-style pre_plan path skips Scout; plan_source = "pre_plan"."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    forced = _Plan(subtasks=[_Sub("general", "x", [])])
    result = await n.ask("anything", pre_plan=forced)
    assert result.timings.plan_source == "pre_plan"
    assert result.timings.scout_seconds is None


@pytest.mark.asyncio
async def test_ask_subtask_seconds_appear_per_outcome(monkeypatch) -> None:
    """Each outcome contributes one (task_type, seconds) entry, in plan order."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    async def fake_plan(self, *a, **kw):  # noqa: ANN001, ANN201, ARG002
        return _Plan(
            subtasks=[
                _Sub("research", "step 1", []),
                _Sub("analyze", "step 2", ["research"]),
            ]
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)

    result = await n.ask("help me figure out something complex enough")
    types = [tt for tt, _ in result.timings.subtask_seconds]
    assert types == ["research", "analyze"]
    # Each subtask wall-clock should be >= 0 (could be ~0 with fake_run).
    for _, secs in result.timings.subtask_seconds:
        assert secs >= 0.0


@pytest.mark.asyncio
async def test_ask_counts_refusal_retries(monkeypatch) -> None:
    """When an attempt's failure_reason == "user_serving_refusal", count it.

    The 0.1.40 retry-after-refusal path is the most common cause of
    extra latency on real asks. Surfacing the count lets users tell
    "the citizens had to try twice" from "this was just slow".
    """
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    calls = {"n": 0}

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        # First call returns a refusal; second succeeds. We synthesize
        # two attempts on the outcome by returning a result tagged with
        # failure_reason — executor's retry path uses agent.py's
        # downgrade path, but for this unit test we short-circuit by
        # injecting both attempt records via the on_progress shim.
        calls["n"] += 1
        # Return success but with failure_reason set on the FIRST call —
        # executor will see it as a successful attempt-with-marker.
        if calls["n"] == 1:
            return _ok_result(
                task_type, failure_reason="user_serving_refusal"
            )
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    forced = _Plan(subtasks=[_Sub("general", "do it", [])])
    result = await n.ask("anything", pre_plan=forced)
    # At least one attempt had the refusal marker.
    assert result.timings.refusal_retry_count >= 1


# --- SessionTurn schema (forward/back compat) ------------------------------


def test_session_turn_to_dict_includes_timings_when_set() -> None:
    turn = SessionTurn(
        ts=1.0,
        request="x",
        final_output="y",
        timings={"total_seconds": 14.8, "plan_source": "scout"},
    )
    d = turn.to_dict()
    assert d["timings"] == {"total_seconds": 14.8, "plan_source": "scout"}


def test_session_turn_to_dict_omits_timings_when_empty() -> None:
    """Backward-compatibility: turns without a timings dict shouldn't
    write the field at all, so older JSONL files stay byte-identical."""
    turn = SessionTurn(ts=1.0, request="x", final_output="y")
    d = turn.to_dict()
    assert "timings" not in d


def test_session_turn_from_dict_handles_missing_timings() -> None:
    """Older v0.1.43 JSONL records have no `timings` key — load must
    succeed and default to empty dict."""
    d = {
        "kind": "turn",
        "ts": 1.0,
        "request": "x",
        "final_output": "y",
        "duration": 5.0,
    }
    turn = SessionTurn.from_dict(d)
    assert turn.timings == {}


def test_session_turn_from_dict_loads_timings() -> None:
    d = {
        "kind": "turn",
        "ts": 1.0,
        "request": "x",
        "final_output": "y",
        "duration": 5.0,
        "timings": {"total_seconds": 5.0, "plan_source": "trivial"},
    }
    turn = SessionTurn.from_dict(d)
    assert turn.timings == {"total_seconds": 5.0, "plan_source": "trivial"}


# --- 0.1.47 — clarify_seconds captures the "hidden" pre-Scout cost --------


def test_asktimings_clarify_seconds_defaults_none() -> None:
    """When clarify didn't run, the field is None (not 0.0) — so post-hoc
    analysis can tell "clarify skipped" apart from "clarify ran in 0s"."""
    t = AskTimings()
    assert t.clarify_seconds is None
    assert t.to_dict()["clarify_seconds"] is None


def test_asktimings_clarify_seconds_round_trips() -> None:
    t = AskTimings(clarify_seconds=7.42)
    assert t.to_dict()["clarify_seconds"] == 7.42


@pytest.mark.asyncio
async def test_ask_captures_clarify_seconds_when_clarifier_runs(monkeypatch) -> None:
    """Wire on_clarify into a non-trivial ask → clarify_seconds > 0."""
    import asyncio

    from anthill.core import clarify as clarify_module

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    async def fake_plan(self, *a, **kw):  # noqa: ANN001, ANN201, ARG002
        return _Plan(subtasks=[_Sub("general", "do it", [])])

    monkeypatch.setattr(_Scout, "plan", fake_plan)

    # Stub maybe_clarify to add measurable delay and return the request
    # unchanged. This is the SAME code path Nation.ask hits — we just
    # make its cost visible.
    async def slow_clarify(nation, request, handler):  # noqa: ANN001, ANN201, ARG001
        await asyncio.sleep(0.02)
        return request

    monkeypatch.setattr(clarify_module, "maybe_clarify", slow_clarify)

    async def handler(_q):  # noqa: ANN001, ANN202
        return "ok"

    result = await n.ask(
        "help me figure out a presentation thing for next week",
        on_clarify=handler,
    )
    assert result.timings.clarify_seconds is not None
    assert result.timings.clarify_seconds >= 0.015


@pytest.mark.asyncio
async def test_ask_clarify_seconds_none_on_trivial(monkeypatch) -> None:
    """Trivial requests skip clarify; the field stays None."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    async def handler(_q):  # noqa: ANN001, ANN202
        return "ok"

    # "hi" is trivial → fast_classify == "trivial" → clarify skipped.
    result = await n.ask("hi", on_clarify=handler)
    assert result.timings.clarify_seconds is None
