"""Immune system — detect and isolate misbehaving citizens.

The pieces that make this work were laid earlier:
  v0.3.0  retire/unretire (soft delete)
  v0.5.0  failure.classify_attempt (structured reason)
  v0.5.0  TaskResult.failure_reason
  v0.5.0  Agent.quarantined_at field (this file uses it)

What this module does:

  1. Keep a small in-memory sliding window of recent attempts per
     (citizen, task_type). We don't read history.jsonl on every
     decision — too slow, too IO-heavy. The window updates as
     Nation.run completes.

  2. After each completed attempt, ask: does this citizen's recent
     pattern look pathological? If yes, quarantine.

  3. Provide a hook for periodic "probe" requests — quarantined
     citizens occasionally get tried again, and three consecutive
     successes lifts the quarantine. This is what distinguishes
     quarantine from retirement: it's not a verdict, it's a watchful
     pause.

Pathology heuristics (v0.5.1 — conservative on purpose, easy to tune):
  - Failure rate ≥ FAIL_RATE_THRESHOLD over last WINDOW attempts
  - Of those failures, at least HALF are 'actionable' reasons
    (POLICY_REFUSAL / EMPTY_RESPONSE / MODEL_ERROR / FORMAT_ERROR).
    This filters out environmental hiccups (NETWORK / RATE_LIMIT /
    TIMEOUT) that aren't the citizen's fault.

The immune system is OFF by default — Nation has to flip
`immune_enabled=True` for the auto-quarantine pipeline to engage. Users
can still call `anthill citizen quarantine <id>` manually regardless.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from anthill.core.failure import FailureReason, is_actionable

if TYPE_CHECKING:
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation


# Tunable defaults — conservative. The hard tradeoff is false-quarantine
# (a transient policy refusal isolates a good citizen) vs late-detect
# (a poisoned model keeps running until users complain). Start lenient.
WINDOW_SIZE = 10                  # how many recent attempts we consider
FAIL_RATE_THRESHOLD = 0.6         # >60% failure ⇒ candidate
ACTIONABLE_FRACTION = 0.5         # half of failures must be citizen-attributable
MIN_OBSERVATIONS = 5              # never quarantine before this many tries
PROBE_INTERVAL_SECONDS = 300      # how long between quarantine probes (5min)
PROBE_RELEASE_STREAK = 3          # successes in a row to lift quarantine


@dataclass
class AttemptRecord:
    """One slot in the sliding window."""

    timestamp: float
    success_score: float
    failure_reason: FailureReason | None
    task_type: str

    @property
    def succeeded(self) -> bool:
        return self.success_score > 0 and self.failure_reason is None


@dataclass
class CitizenHealth:
    """Sliding-window health of one citizen across all task types.

    Stored on Nation rather than Agent so the in-memory state stays
    cheap to swap out (e.g. when reloading a nation, we just rebuild
    from history). The deque cap is WINDOW_SIZE — once full, oldest
    falls out.
    """

    agent_id: str
    window: deque[AttemptRecord] = field(default_factory=lambda: deque(maxlen=WINDOW_SIZE))
    # When the citizen has been quarantined, when we last sent a probe.
    last_probe_at: float | None = None
    # Successes since the most recent probe series began. Reset on
    # quarantine, incremented on each successful probe, decremented
    # (set to 0) on any failed probe.
    probe_streak: int = 0

    def record(self, attempt: AttemptRecord) -> None:
        self.window.append(attempt)

    @property
    def observations(self) -> int:
        return len(self.window)

    @property
    def failure_rate(self) -> float:
        if not self.window:
            return 0.0
        fails = sum(1 for a in self.window if not a.succeeded)
        return fails / len(self.window)

    @property
    def actionable_failures(self) -> int:
        return sum(
            1 for a in self.window
            if not a.succeeded
            and a.failure_reason is not None
            and is_actionable(a.failure_reason)
        )

    @property
    def failure_count(self) -> int:
        return sum(1 for a in self.window if not a.succeeded)

    def is_pathological(self) -> bool:
        """The core question. Tunable via the module-level constants."""
        if self.observations < MIN_OBSERVATIONS:
            return False
        if self.failure_rate < FAIL_RATE_THRESHOLD:
            return False
        fails = self.failure_count
        if fails == 0:
            return False
        actionable_fraction = self.actionable_failures / fails
        return actionable_fraction >= ACTIONABLE_FRACTION

    def dominant_reason(self) -> FailureReason | None:
        """Most common failure_reason in the window, or None on tie / empty."""
        from collections import Counter
        reasons = [
            a.failure_reason for a in self.window
            if a.failure_reason is not None
        ]
        if not reasons:
            return None
        counts = Counter(reasons)
        most_common = counts.most_common(2)
        if len(most_common) > 1 and most_common[0][1] == most_common[1][1]:
            return None  # tie — don't claim a single cause
        return most_common[0][0]


# --- the nation-side glue --------------------------------------------------


def record_attempt(
    nation: "Nation",
    agent_id: str,
    task_type: str,
    result: "TaskResult",
) -> CitizenHealth:
    """Push one observation into the per-citizen sliding window.

    Returns the (possibly updated) CitizenHealth so callers can chain
    a quarantine decision in the same place.
    """
    healths: dict[str, CitizenHealth] = nation.citizen_health  # type: ignore[attr-defined]
    health = healths.get(agent_id)
    if health is None:
        health = CitizenHealth(agent_id=agent_id)
        healths[agent_id] = health
    fr: FailureReason | None = None
    if result.failure_reason is not None:
        try:
            fr = FailureReason(result.failure_reason)
        except ValueError:
            fr = FailureReason.UNKNOWN
    health.record(AttemptRecord(
        timestamp=time.time(),
        success_score=result.success_score,
        failure_reason=fr,
        task_type=task_type,
    ))
    return health


def maybe_quarantine(
    nation: "Nation",
    agent: "Agent",
    health: CitizenHealth,
) -> bool:
    """Quarantine the agent if its health window says pathological.

    No-op if the citizen is already quarantined or retired. Returns
    True iff a new quarantine was applied. Caller is responsible for
    persisting the nation afterward.
    """
    if not nation.immune_enabled:
        return False
    if agent.is_retired or agent.is_quarantined:
        return False
    if not health.is_pathological():
        return False
    agent.quarantined_at = time.time()
    reason = health.dominant_reason()
    agent.quarantine_reason = (
        f"{reason.value} dominated last {health.observations} attempts"
        if reason is not None
        else f"failure rate {health.failure_rate:.0%} over last {health.observations}"
    )
    return True


def maybe_probe_release(
    nation: "Nation",
    agent: "Agent",
    health: CitizenHealth,
    result: "TaskResult",
) -> bool:
    """Update probe streak after a probe attempt; release if streak met.

    Called when a quarantined citizen has *been routed to anyway*
    (because the user used `--include-quarantined` or because
    quarantine-aware probing dispatched a low-stakes test). For now we
    treat every successful attempt by a quarantined citizen as a probe.
    Returns True iff the citizen was released.
    """
    if not agent.is_quarantined:
        return False
    health.last_probe_at = time.time()
    if result.success_score > 0 and result.failure_reason is None:
        health.probe_streak += 1
    else:
        health.probe_streak = 0
    if health.probe_streak >= PROBE_RELEASE_STREAK:
        agent.quarantined_at = None
        agent.quarantine_reason = None
        health.probe_streak = 0
        return True
    return False


__all__ = [
    "AttemptRecord",
    "CitizenHealth",
    "WINDOW_SIZE",
    "FAIL_RATE_THRESHOLD",
    "ACTIONABLE_FRACTION",
    "MIN_OBSERVATIONS",
    "PROBE_INTERVAL_SECONDS",
    "PROBE_RELEASE_STREAK",
    "record_attempt",
    "maybe_quarantine",
    "maybe_probe_release",
]
