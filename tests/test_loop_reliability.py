"""0.2.18 — /loop reliability fixes.

Real-session feedback: "loop 触发的也很差". Three diagnosed root
causes, each fixed here:

1. CONSECUTIVE-miss tracking (not "ever missed past iter 3"). The
   old rule killed loops where the model emitted markers fine on
   iter 1..N then forgot once on iter N+1. Now: one good marker
   resets the counter; loop only dies after N consecutive misses.

2. brevity vs loop-marker conflict. 0.2.9's brevity directive says
   "end with 想展开告诉我". 0.2.2's SELF_PACE_INSTRUCTION says
   "end with [[loop:...]]". When both apply, the model usually
   picks brevity and drops the marker. Fix: skip brevity in loop
   iterations.

3. SELF_PACE_INSTRUCTION position. Before, it was appended to the
   USER request, drowning under context blocks. Now it lives in
   the system prompt where it has authority.
"""

from __future__ import annotations

import asyncio
import re

import pytest

import anthill.core.loop as _loop_mod
from anthill.core.agent import Agent
from anthill.core.loop import (
    _NO_MARKER_GIVE_UP_AFTER,
    LoopSpec,
    LoopState,
    parse_loop_decision,
    run_loop,
)
from anthill.core.nation import Nation


@pytest.fixture(autouse=True)
def _fast_miss_wait(monkeypatch):
    """The loop sleeps 5s on every miss to bound cost. Tests would
    otherwise take 30-40s. Zero out the miss wait for the test run."""
    monkeypatch.setattr(_loop_mod, "_NO_MARKER_DEFAULT_WAIT_SECONDS", 0.0)


# --- consecutive-miss tracking -----------------------------------------


def test_one_miss_does_not_stop_loop() -> None:
    """Iter 1 emits marker; iter 2 forgets; iter 3 emits again. Loop
    should NOT die. Pre-0.2.18 it died at iter 2 because iter >= 3
    isn't required for self-paced + any miss past threshold."""
    outputs = [
        "tick 1 [[loop:continue]]",
        "tick 2 — model forgot the marker",
        "tick 3 [[loop:done]]",
    ]
    calls = {"i": 0}

    async def iter_fn(state: LoopState) -> str:
        calls["i"] += 1
        return outputs[state.iteration - 1]

    spec = LoopSpec(
        interval_seconds=0.0,
        request="x",
        self_paced=True,
        max_iterations=10,
    )
    state = asyncio.run(run_loop(spec, on_iteration=iter_fn))
    # Reached all 3 iterations and stopped with "model_done" (not
    # "model_done_implicit"). Counter was 1 after iter 2, reset to
    # 0 at iter 3, then iter 3 said done.
    assert state.iteration == 3
    assert state.stop_reason == "model_done"


def test_two_consecutive_misses_stops_loop() -> None:
    """When the model misses TWO ticks in a row, give up."""
    outputs = [
        "tick 1 [[loop:continue]]",
        "tick 2 — forgot",
        "tick 3 — forgot again",
        "tick 4 — would never run",
    ]

    async def iter_fn(state: LoopState) -> str:
        return outputs[state.iteration - 1]

    spec = LoopSpec(
        interval_seconds=0.0,
        request="x",
        self_paced=True,
        max_iterations=10,
    )
    state = asyncio.run(run_loop(spec, on_iteration=iter_fn))
    assert state.iteration == 3  # stopped at iter 3 on 2nd consecutive miss
    assert state.stop_reason == "model_done_implicit"
    assert state.consecutive_missed_markers >= _NO_MARKER_GIVE_UP_AFTER


def test_good_marker_resets_miss_counter() -> None:
    """Miss, hit, miss, hit, miss, hit — each hit resets so we
    never reach the threshold. Loop just keeps going."""
    outputs = [
        "iter 1 forgot",  # miss
        "iter 2 [[loop:continue]]",  # hit → reset
        "iter 3 forgot",  # miss
        "iter 4 [[loop:continue]]",  # hit → reset
        "iter 5 forgot",  # miss
        "iter 6 [[loop:done]]",  # hit → stop
    ]

    async def iter_fn(state: LoopState) -> str:
        return outputs[state.iteration - 1]

    spec = LoopSpec(
        interval_seconds=0.0,
        request="x",
        self_paced=True,
        max_iterations=10,
    )
    state = asyncio.run(run_loop(spec, on_iteration=iter_fn))
    assert state.iteration == 6
    assert state.stop_reason == "model_done"


def test_done_marker_takes_priority_over_misses() -> None:
    """A 'done' marker should always end the loop cleanly,
    regardless of how many recent misses there were."""
    outputs = [
        "forgot 1",
        "[[loop:done]] task complete",
    ]

    async def iter_fn(state: LoopState) -> str:
        return outputs[state.iteration - 1]

    spec = LoopSpec(
        interval_seconds=0.0,
        request="x",
        self_paced=True,
        max_iterations=10,
    )
    state = asyncio.run(run_loop(spec, on_iteration=iter_fn))
    assert state.iteration == 2
    assert state.stop_reason == "model_done"


# --- _compose_system suppresses brevity in loop -------------------------


def test_compose_system_includes_brevity_outside_loop() -> None:
    """Sanity: outside a loop, brevity directive IS in the system prompt."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    n._in_loop_iteration = False  # type: ignore[attr-defined]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    assert "concise" in sys_prompt.lower() or "under 800" in sys_prompt


def test_compose_system_drops_brevity_in_loop() -> None:
    """0.2.18 — inside a loop iteration, brevity is suppressed."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    n._in_loop_iteration = True  # type: ignore[attr-defined]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    assert "under 800" not in sys_prompt
    # And the loop marker contract IS in there instead.
    assert "[[loop:" in sys_prompt
    assert "done" in sys_prompt
    assert "wait" in sys_prompt
    n._in_loop_iteration = False  # type: ignore[attr-defined]


def test_compose_system_loop_flag_default_off() -> None:
    """A nation that never had the flag set still works (no AttributeError)."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    # Don't set _in_loop_iteration at all.
    sys_prompt = n._compose_system(n.agents[0]) or ""
    # Brevity included by default.
    assert "under 800" in sys_prompt
    # Loop instruction NOT included.
    assert "[[loop:" not in sys_prompt


# --- parse_loop_decision regression sanity ----------------------------


def test_parse_decision_done() -> None:
    decision, cleaned, wait = parse_loop_decision("blah [[loop:done]]")
    assert decision == "done"
    assert "blah" in cleaned
    assert "[[loop:" not in cleaned
    assert wait == 0.0


def test_parse_decision_continue() -> None:
    decision, cleaned, wait = parse_loop_decision("[[loop:continue]] more")
    assert decision == "continue"


def test_parse_decision_wait_with_unit() -> None:
    decision, cleaned, wait = parse_loop_decision("ok [[loop:wait 2m]]")
    assert decision == "wait"
    assert wait == 120.0


def test_parse_decision_none_when_missing() -> None:
    decision, cleaned, wait = parse_loop_decision("just a regular response")
    assert decision == "none"
    assert cleaned == "just a regular response"


# --- behavioral: loop survives one forgetful iteration ----------------


def test_loop_completes_through_intermittent_misses() -> None:
    """Simulates a 'real' chatty loop: model emits 5 iterations, 2 of
    which forget the marker but none are consecutive. Result: loop
    runs to completion (model_done on iter 5)."""
    outputs = [
        "[[loop:continue]] tick 1",
        "tick 2 forgot",  # miss
        "[[loop:continue]] tick 3 back on track",  # reset
        "tick 4 forgot",  # miss (counter=1)
        "[[loop:done]] tick 5",  # reset + done
    ]

    async def iter_fn(state: LoopState) -> str:
        return outputs[state.iteration - 1]

    spec = LoopSpec(
        interval_seconds=0.0,
        request="x",
        self_paced=True,
        max_iterations=10,
    )
    state = asyncio.run(run_loop(spec, on_iteration=iter_fn))
    assert state.iteration == 5
    assert state.stop_reason == "model_done"
