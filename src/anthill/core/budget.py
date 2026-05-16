"""Budget enforcement — stop an ask before it runs away with the wallet.

A three-step plan with retries on flaky models can quietly burn through
$0.50 in 20 seconds. The user notices afterwards, in the costs report.
That's the wrong order: the user wants to say "spend at most 50 cents
on this" up front and have the nation honor it, not apologise for it.

A Budget carries three orthogonal caps — tokens, cost, wall-clock. Any
one of them being non-None is enough to enforce something. The
BudgetTracker is the mutable counterpart: the executor hands it each
attempt's token counts, and asks before each new subtask whether
there's any cap left to spend.

When the tracker says "stop", the executor doesn't crash the ask. It
marks every remaining subtask as 'skipped' with a budget-exhausted
reason so the user still sees whatever partial work was completed.
Partial output is almost always more useful than a clean abort with
nothing to show.

Cost math lives in core/costs.price_for so this module never has to
know about specific model pricing — the only thing it needs is a
model_lookup callable to turn an agent_id into a model name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Literal

from anthill.core.costs import price_for


# Why this subtask was stopped — surfaced in skip_reason on outcomes.
ExhaustionReason = Literal["tokens", "cost", "time"]


@dataclass
class Budget:
    """Caps the user can set for one ask. Any of them being None means uncapped.

    `max_tokens` is total input + output, summed across every attempt of
    every subtask. `max_cost_usd` and `max_seconds` are the analogous
    monetary and wall-clock caps. A Budget with all three None is a
    no-op; in that case BudgetTracker.may_run_next always returns None.
    """

    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_seconds: float | None = None

    def is_empty(self) -> bool:
        """True when no caps are set — caller can skip building a tracker."""
        return (
            self.max_tokens is None
            and self.max_cost_usd is None
            and self.max_seconds is None
        )


class BudgetTracker:
    """Running tally of token/cost/time spend for one ask.

    The executor calls `may_run_next()` before starting each new subtask
    and `record_attempt()` after each attempt completes. Both methods
    are fast (no I/O) — this object is hot-path during multi-subtask
    asks.

    `model_lookup` translates an agent_id into a model name so the
    tracker can resolve per-million prices without the executor
    knowing anything about pricing.
    """

    def __init__(
        self,
        budget: Budget,
        *,
        model_lookup: Callable[[str], str],
    ) -> None:
        self.budget = budget
        self._model_lookup = model_lookup
        self._spent_tokens: int = 0
        self._spent_usd: float = 0.0
        self._started_at: float = time.time()

    @property
    def spent_tokens(self) -> int:
        return self._spent_tokens

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self._started_at

    def record_attempt(
        self,
        agent_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Charge one attempt's tokens against the running tally."""
        self._spent_tokens += int(input_tokens) + int(output_tokens)
        model = self._model_lookup(agent_id)
        in_per_m, out_per_m = price_for(model)
        self._spent_usd += (
            input_tokens * in_per_m / 1_000_000
            + output_tokens * out_per_m / 1_000_000
        )

    def may_run_next(self) -> ExhaustionReason | None:
        """If any cap is exhausted, return the reason; otherwise None.

        Order of checks matters only for the reason string returned —
        once any one cap is blown, the answer is "stop" regardless of
        the others.
        """
        b = self.budget
        if b.max_tokens is not None and self._spent_tokens >= b.max_tokens:
            return "tokens"
        if b.max_cost_usd is not None and self._spent_usd >= b.max_cost_usd:
            return "cost"
        if b.max_seconds is not None and self.elapsed_seconds >= b.max_seconds:
            return "time"
        return None

    def remaining_summary(self) -> str:
        """Human-readable line: 'spent $0.02 / $0.50 · 1.2k tokens · 4.1s'.

        Used by `anthill ask` to render a footer when a budget was set,
        so the user can see how close they came to the cap.
        """
        parts = [f"spent ${self._spent_usd:.4f}"]
        if self.budget.max_cost_usd is not None:
            parts[-1] = f"spent ${self._spent_usd:.4f} / ${self.budget.max_cost_usd:.4f}"
        parts.append(f"{self._spent_tokens:,} tokens")
        if self.budget.max_tokens is not None:
            parts[-1] = f"{self._spent_tokens:,} / {self.budget.max_tokens:,} tokens"
        parts.append(f"{self.elapsed_seconds:.1f}s")
        if self.budget.max_seconds is not None:
            parts[-1] = f"{self.elapsed_seconds:.1f}s / {self.budget.max_seconds:.0f}s"
        return " · ".join(parts)


def reason_label(reason: ExhaustionReason) -> str:
    """Pretty label for a skip_reason string. Stable across versions."""
    return {
        "tokens": "token budget exhausted",
        "cost": "cost budget exhausted",
        "time": "time budget exhausted",
    }[reason]


@dataclass
class BudgetSnapshot:
    """A point-in-time summary of a tracker's spend.

    Returned from execute_plan so callers (CLI, REPL) can render a
    final footer without needing the live tracker object.
    """

    tokens: int
    cost_usd: float
    elapsed_seconds: float
    exhausted: ExhaustionReason | None = None
    summary: str = ""


def snapshot(tracker: BudgetTracker) -> BudgetSnapshot:
    return BudgetSnapshot(
        tokens=tracker.spent_tokens,
        cost_usd=tracker.spent_usd,
        elapsed_seconds=tracker.elapsed_seconds,
        exhausted=tracker.may_run_next(),
        summary=tracker.remaining_summary(),
    )


__all__ = [
    "Budget",
    "BudgetTracker",
    "BudgetSnapshot",
    "ExhaustionReason",
    "reason_label",
    "snapshot",
]
