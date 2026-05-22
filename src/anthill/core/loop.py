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
    # 0.2.2 — self-paced mode. When True, the model decides the
    # next cadence via a [[loop:...]] marker at end of each iteration.
    # interval_seconds is ignored in this mode (the model's decision
    # wins). Triggered from the REPL by typing `/loop <ask>` without
    # an interval token.
    self_paced: bool = False


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


# --- self-paced mode (0.2.2) --------------------------------------------
#
# When LoopSpec.self_paced is True, the model decides the cadence of
# the next iteration via a marker at end of output:
#
#   [[loop:done]]           — task complete, stop the loop
#   [[loop:continue]]       — no wait, run again immediately
#   [[loop:wait 60]]        — pause N seconds (default unit: seconds)
#   [[loop:wait 5m]]        — also accepts m/h suffixes
#
# We append SELF_PACE_INSTRUCTION to the user's request when
# self_paced=True; the final subtask sees it and ends with the marker.
# After each iteration, parse_loop_decision strips the marker from the
# displayed output AND tells run_loop what to do next.
#
# Why marker-in-output instead of a second LLM call?
#   - Cheaper (1 call/iter vs 2)
#   - Simpler — no separate decision agent prompt
#   - The model has full context of what just happened, doesn't need
#     a second pass to judge "should we continue?"

SELF_PACE_INSTRUCTION = """\

==================
LOOP CONTROL: this ask runs in a self-paced loop. At the END of your
response, on its own line, include EXACTLY ONE of:

  [[loop:done]]           — the task is complete; stop the loop
  [[loop:continue]]       — keep going immediately, no wait
  [[loop:wait <N>]]       — pause N seconds (e.g. [[loop:wait 60]])
                            also accepts m/h units: [[loop:wait 5m]]

Choose `done` when the task succeeded OR when further iterations
won't help. Choose `wait N` when you're polling something that won't
change instantly (deploy progress, log tailing, PR merge). Choose
`continue` for fast iterative refinement.
If you omit the marker, the loop will stop after a few iterations.
=================="""


# Capture either a bare decision (done/continue) or "wait <number>[unit]".
_LOOP_MARKER_RE = re.compile(
    r"""
    \[\[\s*loop\s*:\s*
      (?:
        (?P<decision>done|continue)
        |
        wait\s+(?P<wait_num>\d+(?:\.\d+)?)\s*(?P<wait_unit>[smh])?
      )
    \s*\]\]
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_loop_decision(text: str) -> tuple[str, str, float]:
    """Parse a self-pace marker out of an iteration's output.

    Returns (decision, cleaned_output, wait_seconds):
      decision in {"done", "continue", "wait", "none"}
      cleaned_output = `text` with the marker line stripped
      wait_seconds = seconds to sleep when decision == "wait", else 0.0

    "none" means no marker found. Caller decides the default behavior
    (run_loop assumes done after a few iterations, otherwise waits 5s).

    The first marker wins if a buggy model emits multiple. We strip
    every match to keep the displayed output clean.
    """
    if not text:
        return "none", text, 0.0
    matches = list(_LOOP_MARKER_RE.finditer(text))
    if not matches:
        return "none", text, 0.0

    first = matches[0]
    if first.group("decision"):
        decision = first.group("decision").lower()
        wait_seconds = 0.0
    else:
        decision = "wait"
        n = float(first.group("wait_num"))
        unit = (first.group("wait_unit") or "s").lower()
        wait_seconds = n * {"s": 1.0, "m": 60.0, "h": 3600.0}[unit]

    # Strip ALL markers (just in case there are stragglers).
    cleaned = _LOOP_MARKER_RE.sub("", text).rstrip()
    return decision, cleaned, wait_seconds


# How many iterations of a self-paced loop to tolerate without a
# marker before assuming the model has implicitly declared done.
# Below this we treat "no marker" as "continue with a brief wait" so
# we don't kill the loop on a single forgetful response.
_NO_MARKER_GIVE_UP_AFTER = 3
# Default sleep when self-paced loop got no marker but isn't yet at
# the give-up threshold. Keeps wasted token cost bounded.
_NO_MARKER_DEFAULT_WAIT_SECONDS = 5.0


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

            # 0.2.2 — self-paced cadence. Pull the [[loop:...]] marker
            # out of the output; that decides what happens next. The
            # cleaned output (marker stripped) is what we record /
            # display.
            next_sleep: float = spec.interval_seconds
            if spec.self_paced:
                decision, cleaned, wait_secs = parse_loop_decision(output)
                output = cleaned  # user sees this; no marker noise
                if decision == "done":
                    state.record_output(output)
                    state.stop_reason = "model_done"
                    if on_progress is not None:
                        on_progress(state, "tick_end")
                    break
                if decision == "wait":
                    next_sleep = wait_secs
                elif decision == "continue":
                    next_sleep = 0.0
                else:  # "none" — model forgot the marker
                    if state.iteration >= _NO_MARKER_GIVE_UP_AFTER:
                        # Model has had multiple chances; assume done
                        # rather than burn more iterations.
                        state.record_output(output)
                        state.stop_reason = "model_done_implicit"
                        if on_progress is not None:
                            on_progress(state, "tick_end")
                        break
                    next_sleep = _NO_MARKER_DEFAULT_WAIT_SECONDS

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
            if state.iteration < spec.max_iterations and next_sleep > 0:
                try:
                    await asyncio.sleep(next_sleep)
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
