"""0.2.1 — `/loop` mechanism.

Hermes has a loop primitive: "run this prompt on a recurring interval
until I stop it or the work is done." Useful for:

  - "watch this deploy until it succeeds"
  - "check the PR status every minute"
  - "keep refining this draft, comparing to prior iteration"
  - "monitor system load until back below threshold"

This module is the engine; the REPL wraps it as a `/loop` command.

MVP scope (0.2.1):
  - Fixed-interval foreground loop: `/loop 30s <ask>`
  - Ctrl+C clean termination
  - Each iteration sees the prior iteration's output as context
  - Iteration count + interval echo for transparency

Out of scope (later versions):
  - Model self-pacing (0.2.2)
  - `until <condition>` clause (0.2.3)
  - Background loops (compose with /bg)
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

# Maximum iterations before automatic stop. Protects against
# accidental infinite loops on a sleep-0 interval. Power users can
# raise this if they really want, but the default keeps a runaway
# loop from burning $100 of token cost in a few hours.
DEFAULT_MAX_ITERATIONS: int = 100

# How many prior outputs to feed back as context. Just the immediate
# previous output is usually enough ("did anything change since last
# tick?"). More than 3 starts to dominate the prompt budget.
DEFAULT_HISTORY_WINDOW: int = 1


@dataclass
class LoopSpec:
    """One loop's configuration."""

    interval_seconds: float
    request: str
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    history_window: int = DEFAULT_HISTORY_WINDOW


@dataclass
class LoopState:
    """Mutable state across iterations."""

    spec: LoopSpec
    iteration: int = 0
    started_at: float = field(default_factory=time.time)
    # Rolling window of outputs from prior iterations. New output
    # appended at end; truncated to spec.history_window length.
    prior_outputs: list[str] = field(default_factory=list)
    # Reasons we might stop, in order of priority:
    #   user_stop      — Ctrl+C / explicit stop
    #   max_iters      — hit DEFAULT_MAX_ITERATIONS
    #   model_done     — (0.2.2) model declared task complete
    #   error          — unrecoverable error in iteration
    stop_reason: str | None = None

    def record_output(self, output: str) -> None:
        """Append iteration output; trim to history_window."""
        self.prior_outputs.append(output)
        if len(self.prior_outputs) > self.spec.history_window:
            self.prior_outputs = self.prior_outputs[-self.spec.history_window:]

    def request_with_context(self) -> str:
        """Build the request the next iteration sees.

        Iteration 1: just the spec's request.
        Iteration 2+: spec request prepended with prior output(s)
        wrapped in <prior_iteration> for clarity.
        """
        if not self.prior_outputs:
            return self.spec.request
        history_blocks: list[str] = []
        # Number from oldest-first so the model can see "tick N produced"
        # in chronological order.
        start_iter = self.iteration - len(self.prior_outputs) + 1
        for offset, out in enumerate(self.prior_outputs):
            history_blocks.append(
                f"<prior_iteration n={start_iter + offset}>\n"
                f"{out}\n"
                f"</prior_iteration>"
            )
        return (
            "\n".join(history_blocks)
            + "\n\n"
            + self.spec.request
        )


# --- interval parsing ----------------------------------------------------


_INTERVAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h)?\s*$", re.IGNORECASE)


def parse_interval(text: str) -> float | None:
    """Parse '30s' / '5m' / '2h' / '45' (seconds default) → seconds.

    Returns None when the input can't be parsed. The REPL surfaces
    this as 'unknown interval; try 30s / 5m / 1h'.
    """
    if not text:
        return None
    m = _INTERVAL_RE.match(text)
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0}
    return n * multipliers[unit]


def format_interval(seconds: float) -> str:
    """30 → '30s', 90 → '1m30s', 3700 → '1h1m40s'. Used for echo lines."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    parts: list[str] = []
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return "".join(parts)


# --- execution -----------------------------------------------------------


# Callback signature: (state) -> awaitable that runs ONE iteration and
# returns the output string. Caller provides this so the loop module
# stays unaware of Nation / REPL details.
IterationFn = Callable[[LoopState], Awaitable[str]]

# Callback for stop checks called BETWEEN iterations (after a tick
# returns but before sleeping). Returning a non-None reason stops the
# loop. Useful for the REPL to check "did the user hit Ctrl+C?".
StopCheckFn = Callable[[LoopState], Optional[str]]


async def run_loop(
    spec: LoopSpec,
    *,
    on_iteration: IterationFn,
    on_progress: Callable[[LoopState, str], None] | None = None,
    stop_check: StopCheckFn | None = None,
) -> LoopState:
    """Run the loop. Returns the final LoopState with stop_reason set.

    Cancellation:
      - The caller's asyncio task can be cancelled; we catch and set
        stop_reason='user_stop'.
      - stop_check fires after each iteration; non-None reason stops.
      - max_iterations cap stops with 'max_iters'.

    No exceptions propagate out — iteration errors become
    stop_reason='error' and the error text is the last entry of
    prior_outputs (so the user sees what went wrong).
    """
    state = LoopState(spec=spec)
    try:
        while state.iteration < spec.max_iterations:
            state.iteration += 1
            if on_progress is not None:
                on_progress(state, "tick_start")
            try:
                output = await on_iteration(state)
            except asyncio.CancelledError:
                state.stop_reason = "user_stop"
                raise
            except Exception as e:  # noqa: BLE001
                state.record_output(
                    f"[loop iteration {state.iteration} errored: "
                    f"{type(e).__name__}: {e}]"
                )
                state.stop_reason = "error"
                break
            state.record_output(output)
            if on_progress is not None:
                on_progress(state, "tick_end")

            # Inter-iteration stop check (e.g. /loop stop in another
            # thread, or a sentinel in the output).
            if stop_check is not None:
                reason = stop_check(state)
                if reason is not None:
                    state.stop_reason = reason
                    break

            # Sleep before next iteration unless we already hit max.
            if state.iteration < spec.max_iterations:
                try:
                    await asyncio.sleep(spec.interval_seconds)
                except asyncio.CancelledError:
                    state.stop_reason = "user_stop"
                    raise
        else:
            state.stop_reason = "max_iters"
    except asyncio.CancelledError:
        # Propagate so any caller using cancellation also sees it,
        # but record the reason on state for telemetry.
        if state.stop_reason is None:
            state.stop_reason = "user_stop"
        # Re-raise so REPL knows the task was cancelled.
        raise
    return state
