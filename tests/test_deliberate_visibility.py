"""0.1.27 — deliberation phase visibility.

User feedback: "深度思考这个的过程也是看不到的，这个是可以实时进度
查看的." Between rounds Anthill went silent for 5-15 seconds while
the critique LLM thought, then suddenly fired round 2. Now the
REPL surfaces every internal phase via two new callbacks on
``deliberate()``:

  on_phase(name, payload) — discrete events at phase transitions
    "critique_start" {round, weakest}
    "critique_done"  {round, critic_id, critique}
    "refine_start"   {round}

  on_critique_token(delta) — streams the critique LLM's tokens
                              while it's being generated

Tests cover that both callbacks fire on a 2-round deliberation and
that omitting them keeps the old quiet behavior (backward compat).
"""

from __future__ import annotations

import pytest


# --- shared fixtures ------------------------------------------------------


def _make_test_nation(monkeypatch):
    """Stub Nation with a scripted Scout and run() so deliberate() can
    drive it without real LLM calls."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    n = Nation(name="t")
    n.use_judge = False  # keep results deterministic
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_plan(self, request, **kwargs):
        return _Plan(subtasks=[_Sub("research", request, [])])

    captured_on_token = []
    call_counter = {"n": 0}

    async def fake_run(task_type, prompt, **kwargs):
        # Record on_token so tests can verify the critique was streamed.
        captured_on_token.append(kwargs.get("on_token"))
        call_counter["n"] += 1
        # Simulate streaming for the critique (task_type=='review') so
        # the critique callback actually gets called.
        on_tok = kwargs.get("on_token")
        if on_tok is not None and task_type == "review":
            for chunk in ("missing X. ", "needs Y. ", "also Z."):
                await on_tok(chunk)
        return TaskResult(
            task_id=f"t{call_counter['n']}",
            agent_id="ant-1",
            task_type=task_type,
            output=f"round-{call_counter['n']}",
            success_score=1.0,
            duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]
    return n, captured_on_token


# --- tests ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_callback_fires_in_order(monkeypatch) -> None:
    """A 2-round deliberation should emit critique_start →
    critique_done → refine_start in that order."""
    from anthill.core.deliberate import deliberate

    n, _ = _make_test_nation(monkeypatch)
    events: list[tuple[str, dict]] = []

    async def on_phase(name, payload):
        events.append((name, dict(payload)))

    await deliberate(
        n, "research the X protocol in depth",
        max_rounds=2,
        quality_threshold=2.0,  # impossible to converge → forces round 2
        on_phase=on_phase,
    )
    names = [e[0] for e in events]
    # Round 2 entered ⇒ all three phase events fire.
    assert "critique_start" in names
    assert "critique_done" in names
    assert "refine_start" in names
    # Order preserved.
    assert names.index("critique_start") < names.index("critique_done") < names.index("refine_start")


@pytest.mark.asyncio
async def test_critique_token_callback_streams_critique(monkeypatch) -> None:
    """on_critique_token receives the deltas while the critique runs."""
    from anthill.core.deliberate import deliberate

    n, _ = _make_test_nation(monkeypatch)
    received: list[str] = []

    async def on_token(delta):
        received.append(delta)

    await deliberate(
        n, "research the X protocol in depth",
        max_rounds=2,
        quality_threshold=2.0,
        on_critique_token=on_token,
    )
    assert received == ["missing X. ", "needs Y. ", "also Z."]


@pytest.mark.asyncio
async def test_critique_token_threaded_through_nation_run(monkeypatch) -> None:
    """Sanity: deliberate must pass on_critique_token down to
    nation.run() for the 'review' task. Without this thread, the
    stream never reaches the provider layer."""
    from anthill.core.deliberate import deliberate

    n, captured = _make_test_nation(monkeypatch)

    async def on_token(_delta):
        pass

    await deliberate(
        n, "research the X protocol in depth",
        max_rounds=2,
        quality_threshold=2.0,
        on_critique_token=on_token,
    )
    # captured[0] is round-1 ask call (on_token=None expected),
    # captured[1] is the critique call (on_token must be set),
    # captured[2] is round-2 refine (on_token=None expected).
    assert any(cb is on_token for cb in captured), (
        "critique call did not receive on_critique_token — token "
        "streaming for deliberation is broken"
    )


@pytest.mark.asyncio
async def test_phase_callback_omitted_keeps_old_quiet_behavior(monkeypatch) -> None:
    """Passing no on_phase shouldn't crash and shouldn't make
    deliberate run differently — backward compat."""
    from anthill.core.deliberate import deliberate

    n, _ = _make_test_nation(monkeypatch)
    delib = await deliberate(
        n, "research the X protocol in depth",
        max_rounds=2,
        quality_threshold=2.0,
        # no on_phase, no on_critique_token
    )
    # Round 2 ran (it converges nowhere by design here, so max_rounds wins).
    assert delib.total_rounds == 2


@pytest.mark.asyncio
async def test_critique_failure_still_emits_done(monkeypatch) -> None:
    """When the critique call raises, on_phase still gets a
    critique_done event so the REPL can render an error line
    instead of staying half-open."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.deliberate import deliberate
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    n = Nation(name="t")
    n.use_judge = False
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_plan(self, request, **kwargs):
        return _Plan(subtasks=[_Sub("research", request, [])])

    call_n = {"i": 0}

    async def fake_run(task_type, prompt, **kwargs):
        call_n["i"] += 1
        # The critique call (task_type='review') raises; rounds 1 + 2
        # succeed normally.
        if task_type == "review":
            raise RuntimeError("critic offline")
        return TaskResult(
            task_id=f"t{call_n['i']}",
            agent_id="ant-1",
            task_type=task_type,
            output=f"round-{call_n['i']}",
            success_score=1.0,
            duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    events: list[str] = []

    async def on_phase(name, payload):
        events.append(name)

    await deliberate(
        n, "research the X protocol in depth",
        max_rounds=2,
        quality_threshold=2.0,
        on_phase=on_phase,
    )
    assert "critique_start" in events
    assert "critique_done" in events
    assert "refine_start" in events  # refine still runs with placeholder critique
