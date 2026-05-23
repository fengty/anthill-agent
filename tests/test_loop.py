"""0.2.1 — `/loop` engine tests.

The engine is the recurring-ask primitive. Each iteration runs one
async callback (in real use: nation.ask), feeds the prior output back
as context for the next iteration, and stops on cancellation /
max_iters / error / explicit stop_check.

Tests verify the pure-engine behavior. The REPL wiring is exercised
in test_inline_auth_prompt style for follow-on commits.
"""

from __future__ import annotations

import asyncio

import pytest

from anthill.core.loop import (
    DEFAULT_HISTORY_WINDOW,
    DEFAULT_MAX_ITERATIONS,
    LoopSpec,
    LoopState,
    format_interval,
    parse_interval,
    run_loop,
)


# --- parse_interval -----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30s", 30.0),
        ("30", 30.0),         # default unit = seconds
        ("5m", 300.0),
        ("2h", 7200.0),
        ("0.5m", 30.0),       # fractional ok
        ("  10s  ", 10.0),    # whitespace tolerant
        ("10S", 10.0),        # case insensitive
        ("1H", 3600.0),
    ],
)
def test_parse_interval_valid(text, expected) -> None:
    assert parse_interval(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "thirty seconds",
        "30 minutes",     # space + word
        "5x",             # unknown unit
        "abc",
        "5m5s",           # compound not supported in MVP
        "-30",            # negative — regex doesn't accept
    ],
)
def test_parse_interval_invalid_returns_none(text) -> None:
    assert parse_interval(text) is None


# --- format_interval ----------------------------------------------------


@pytest.mark.parametrize(
    "secs,formatted",
    [
        (0, "0s"),
        (30, "30s"),
        (60, "1m"),
        (90, "1m30s"),
        (3600, "1h"),
        (3700, "1h1m40s"),
        (7200, "2h"),
    ],
)
def test_format_interval(secs, formatted) -> None:
    assert format_interval(secs) == formatted


# --- LoopState.request_with_context ------------------------------------


def test_request_with_context_first_iteration() -> None:
    """No prior output → just return the spec request."""
    state = LoopState(spec=LoopSpec(interval_seconds=30, request="check git"))
    state.iteration = 1
    assert state.request_with_context() == "check git"


def test_request_with_context_with_prior() -> None:
    """Prior outputs prepended as <prior_iteration n=N> blocks."""
    state = LoopState(
        spec=LoopSpec(
            interval_seconds=30, request="check git", history_window=2
        )
    )
    state.iteration = 3
    state.record_output("clean")
    state.record_output("one file modified")
    out = state.request_with_context()
    assert "<prior_iteration n=2>" in out
    assert "clean" in out
    assert "<prior_iteration n=3>" in out
    assert "one file modified" in out
    assert "check git" in out


def test_record_output_caps_at_history_window() -> None:
    """Prior outputs trimmed to window size (default 1)."""
    state = LoopState(
        spec=LoopSpec(
            interval_seconds=1,
            request="x",
            history_window=2,
        )
    )
    for o in ["a", "b", "c", "d"]:
        state.record_output(o)
    # Window = 2 → only the last two survive.
    assert state.prior_outputs == ["c", "d"]


# --- run_loop -----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_loop_stops_at_max_iterations() -> None:
    """When the loop has no other reason to stop, it caps at
    max_iterations and reports the reason."""
    spec = LoopSpec(
        interval_seconds=0.001,  # near-zero sleep for test speed
        request="tick",
        max_iterations=3,
    )

    counter = {"n": 0}

    async def fake_iteration(state):
        counter["n"] += 1
        return f"tick {counter['n']}"

    final = await run_loop(spec, on_iteration=fake_iteration)
    assert final.iteration == 3
    assert final.stop_reason == "max_iters"
    assert counter["n"] == 3


@pytest.mark.asyncio
async def test_run_loop_stops_on_user_cancel() -> None:
    """When the task is cancelled (Ctrl+C in the REPL), stop_reason
    becomes 'user_stop' and CancelledError propagates."""
    spec = LoopSpec(interval_seconds=10, request="tick", max_iterations=50)

    async def slow_iteration(state):
        await asyncio.sleep(10)
        return "should be cancelled"

    async def driver():
        task = asyncio.create_task(
            run_loop(spec, on_iteration=slow_iteration)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await driver()


@pytest.mark.asyncio
async def test_run_loop_stops_on_stop_check_signal() -> None:
    """stop_check returning non-None terminates the loop after the
    current iteration."""
    spec = LoopSpec(interval_seconds=0.001, request="x", max_iterations=10)

    async def fake_iteration(state):
        return f"output {state.iteration}"

    def stop_after_two(state):
        return "user_stop" if state.iteration >= 2 else None

    final = await run_loop(
        spec,
        on_iteration=fake_iteration,
        stop_check=stop_after_two,
    )
    assert final.stop_reason == "user_stop"
    assert final.iteration == 2


@pytest.mark.asyncio
async def test_run_loop_catches_iteration_errors() -> None:
    """An exception in on_iteration becomes stop_reason='error', the
    error text is recorded as the last output, and the loop ends —
    no exception propagates out."""
    spec = LoopSpec(interval_seconds=0.001, request="x", max_iterations=10)

    async def boom(state):
        if state.iteration == 2:
            raise RuntimeError("kaboom")
        return "ok"

    final = await run_loop(spec, on_iteration=boom)
    assert final.stop_reason == "error"
    assert final.iteration == 2
    # Error text recorded in last output for user visibility.
    assert "kaboom" in final.prior_outputs[-1]


@pytest.mark.asyncio
async def test_run_loop_feeds_prior_output_to_next_iteration() -> None:
    """Each tick's output becomes context for the next one — that's
    the whole point of the loop."""
    spec = LoopSpec(
        interval_seconds=0.001,
        request="next?",
        max_iterations=3,
        history_window=1,
    )

    received_contexts: list[str] = []

    async def echo(state):
        received_contexts.append(state.request_with_context())
        return f"output-{state.iteration}"

    await run_loop(spec, on_iteration=echo)

    # Iteration 1: no prior, just request.
    assert received_contexts[0] == "next?"
    # Iteration 2: prior=output-1.
    assert "output-1" in received_contexts[1]
    # Iteration 3: prior=output-2 (window=1 drops output-1).
    assert "output-2" in received_contexts[2]
    assert "output-1" not in received_contexts[2]


@pytest.mark.asyncio
async def test_run_loop_calls_on_progress_for_each_tick() -> None:
    spec = LoopSpec(interval_seconds=0.001, request="x", max_iterations=2)
    phases: list[tuple[int, str]] = []

    async def fake_iteration(state):
        return f"o{state.iteration}"

    def on_progress(state, phase):
        phases.append((state.iteration, phase))

    await run_loop(
        spec, on_iteration=fake_iteration, on_progress=on_progress
    )
    # tick_start + tick_end per iteration.
    assert phases == [
        (1, "tick_start"), (1, "tick_end"),
        (2, "tick_start"), (2, "tick_end"),
    ]


def test_loop_spec_defaults() -> None:
    """Default sanity: max_iterations and history_window stable."""
    spec = LoopSpec(interval_seconds=30, request="x")
    assert spec.max_iterations == DEFAULT_MAX_ITERATIONS
    assert spec.history_window == DEFAULT_HISTORY_WINDOW
    assert spec.self_paced is False


# --- 0.2.2 — self-paced loop --------------------------------------------


from anthill.core.loop import (  # noqa: E402 — import after the basic tests
    SELF_PACE_INSTRUCTION,
    parse_loop_decision,
)


# parse_loop_decision: marker grammar


def test_parse_done_marker() -> None:
    decision, cleaned, wait = parse_loop_decision(
        "All good now.\n[[loop:done]]"
    )
    assert decision == "done"
    assert wait == 0.0
    assert "[[loop:" not in cleaned
    assert "All good now." in cleaned


def test_parse_continue_marker() -> None:
    decision, cleaned, wait = parse_loop_decision(
        "Still building...\n[[loop:continue]]"
    )
    assert decision == "continue"
    assert wait == 0.0
    assert "[[loop:" not in cleaned


def test_parse_wait_seconds_marker() -> None:
    decision, cleaned, wait = parse_loop_decision(
        "Waiting for deploy.\n[[loop:wait 60]]"
    )
    assert decision == "wait"
    assert wait == 60.0


def test_parse_wait_minutes_marker() -> None:
    decision, cleaned, wait = parse_loop_decision(
        "Need to wait.\n[[loop:wait 5m]]"
    )
    assert decision == "wait"
    assert wait == 300.0


def test_parse_wait_hours_marker() -> None:
    decision, cleaned, wait = parse_loop_decision(
        "Long poll.\n[[loop:wait 1h]]"
    )
    assert decision == "wait"
    assert wait == 3600.0


def test_parse_no_marker_returns_none_decision() -> None:
    decision, cleaned, wait = parse_loop_decision("Just an output, no marker.")
    assert decision == "none"
    assert cleaned == "Just an output, no marker."
    assert wait == 0.0


def test_parse_marker_case_insensitive() -> None:
    decision, _, _ = parse_loop_decision("[[LOOP:DONE]]")
    assert decision == "done"


def test_parse_marker_strips_all_occurrences() -> None:
    """Buggy model might emit multiple markers — strip all, but
    decision = first one."""
    text = "[[loop:continue]] some text [[loop:wait 30]]"
    decision, cleaned, wait = parse_loop_decision(text)
    assert decision == "continue"  # first wins
    assert wait == 0.0
    assert "[[loop:" not in cleaned


def test_parse_empty_text() -> None:
    decision, cleaned, wait = parse_loop_decision("")
    assert decision == "none"
    assert cleaned == ""


# Integration: run_loop in self_paced mode


@pytest.mark.asyncio
async def test_self_paced_stops_on_done_marker() -> None:
    """Model emits [[loop:done]] → loop stops, stop_reason='model_done'."""
    spec = LoopSpec(
        interval_seconds=0.0,
        request="poll deploy",
        self_paced=True,
        max_iterations=10,
    )

    iter_count = {"n": 0}

    async def fake_iteration(state):
        iter_count["n"] += 1
        if iter_count["n"] >= 3:
            return "deploy succeeded.\n[[loop:done]]"
        return f"still building (iter {iter_count['n']}).\n[[loop:wait 1]]"

    final = await run_loop(spec, on_iteration=fake_iteration)
    assert final.stop_reason == "model_done"
    assert final.iteration == 3
    # Marker stripped from displayed output.
    assert "[[loop:" not in final.prior_outputs[-1]
    assert "deploy succeeded" in final.prior_outputs[-1]


@pytest.mark.asyncio
async def test_self_paced_respects_continue() -> None:
    """[[loop:continue]] = no sleep before next iteration."""
    spec = LoopSpec(
        interval_seconds=999.0,  # very long — would be obvious if used
        request="x",
        self_paced=True,
        max_iterations=3,
    )

    import time as _t
    started = _t.time()

    async def fake_iteration(state):
        if state.iteration < 3:
            return "go.\n[[loop:continue]]"
        return "done.\n[[loop:done]]"

    final = await run_loop(spec, on_iteration=fake_iteration)
    elapsed = _t.time() - started
    # 3 iterations with continue (no sleep) should finish in well
    # under 1 second; if we'd honored interval_seconds=999 it'd hang
    # for ~16 minutes.
    assert elapsed < 1.0
    assert final.stop_reason == "model_done"


@pytest.mark.asyncio
async def test_self_paced_implicit_done_after_consecutive_missing_markers() -> None:
    """0.2.18 — model forgot the marker CONSECUTIVELY past the give-up
    threshold → assume done. The threshold dropped from 3 (counted by
    iteration) to 2 (counted by consecutive misses) so we stop sooner
    on a model that NEVER emits markers, but DON'T stop on a model
    that just had one slip-up mid-run."""
    spec = LoopSpec(
        interval_seconds=0.0,
        request="x",
        self_paced=True,
        max_iterations=10,
    )

    async def no_marker(state):
        return "I have an opinion but I forgot to add the marker."

    final = await run_loop(spec, on_iteration=no_marker)
    assert final.stop_reason == "model_done_implicit"
    assert final.iteration == 2  # 2 consecutive misses


@pytest.mark.asyncio
async def test_self_paced_marker_stripped_from_prior_output() -> None:
    """Subsequent iterations see the CLEANED prior output (no marker
    noise polluting the context)."""
    spec = LoopSpec(
        interval_seconds=0.0,
        request="next?",
        self_paced=True,
        max_iterations=2,
        history_window=1,
    )

    received: list[str] = []

    async def fake_iteration(state):
        received.append(state.request_with_context())
        if state.iteration == 1:
            return "iteration one work.\n[[loop:continue]]"
        return "iteration two work.\n[[loop:done]]"

    await run_loop(spec, on_iteration=fake_iteration)

    # Iter 2's request contains prior output WITHOUT the marker.
    assert "iteration one work" in received[1]
    assert "[[loop:continue]]" not in received[1]
    assert "[[loop:" not in received[1]


def test_self_pace_instruction_documented() -> None:
    """The instruction text is the contract with the model — make sure
    all three decision markers are documented in it."""
    assert "[[loop:done]]" in SELF_PACE_INSTRUCTION
    assert "[[loop:continue]]" in SELF_PACE_INSTRUCTION
    assert "[[loop:wait" in SELF_PACE_INSTRUCTION
