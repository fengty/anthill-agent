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
