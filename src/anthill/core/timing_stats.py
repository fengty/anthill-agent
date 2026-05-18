"""0.1.52 — aggregate per-task_type latency from session JSONL.

Reads the `timings` field 0.1.44/47 started writing to session
turn records and computes:
  - median + p90 per task_type
  - median Scout / Clarify / total
  - "slow" tag for task_types whose median exceeds SLOW_TASK_SECONDS

Surfaced by the `/timing` REPL command so the user can see where
the seconds actually go across recent sessions without grepping
JSONL by hand.

Pure functions; no I/O on its own. Callers pass already-loaded
SessionTurn lists or raw dicts. Same shape as skill_stats.py —
makes it cheap to test and reuse from non-REPL contexts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# Threshold above which we tag a task_type as "slow" in the
# /timing breakdown. Heuristic: 15s feels noticeably long for an
# interactive REPL — that's the boundary where users start
# checking if it's hung.
SLOW_TASK_SECONDS: float = 15.0

# Minimum sample size before we'll even compute aggregate stats
# for a task_type. With 1-2 samples the median is meaningless and
# we'd surface noise as guidance.
MIN_SAMPLES: int = 3


@dataclass(frozen=True)
class TaskTypeStat:
    """Per-task_type latency summary."""

    task_type: str
    samples: int
    median_seconds: float
    p90_seconds: float

    @property
    def is_slow(self) -> bool:
        return self.median_seconds >= SLOW_TASK_SECONDS


@dataclass(frozen=True)
class PhaseStat:
    """Per-phase (Scout / Clarify / total) median over recent asks.

    ``samples`` counts only turns where the phase actually fired —
    turns where Scout was bypassed don't dilute the median with 0s.
    """

    phase: str
    samples: int
    median_seconds: float


@dataclass(frozen=True)
class TimingSummary:
    """The complete /timing readout — one dataclass for easy testing."""

    turn_count: int
    by_task_type: list[TaskTypeStat]
    phases: list[PhaseStat]


def _percentile(values: list[float], pct: float) -> float:
    """Inclusive percentile on a sorted list. Linear interpolation
    between adjacent samples. Empty input → 0.0."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    if len(sorted_v) == 1:
        return sorted_v[0]
    idx = (len(sorted_v) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_v) - 1)
    frac = idx - lo
    return sorted_v[lo] + frac * (sorted_v[hi] - sorted_v[lo])


def _median(values: list[float]) -> float:
    return _percentile(values, 50.0)


def summarize_timings(
    turns: Iterable[dict],
    *,
    min_samples: int = MIN_SAMPLES,
) -> TimingSummary:
    """Aggregate `turns` (raw dicts as written to session JSONL) into
    a `TimingSummary`. Turns without a `timings` field are silently
    skipped — those are pre-0.1.44 records.

    `min_samples` gates which task_types appear in the output: if
    we only have 1-2 samples of "synthesize", showing its median
    would mislead. Bumping this filter is the main knob for
    "how confident is /timing supposed to be?".
    """
    by_task: dict[str, list[float]] = {}
    scout_times: list[float] = []
    clarify_times: list[float] = []
    totals: list[float] = []
    turn_count = 0

    for t in turns:
        timings = t.get("timings") if isinstance(t, dict) else None
        if not timings or not isinstance(timings, dict):
            continue
        turn_count += 1
        total = float(timings.get("total_seconds") or 0.0)
        if total > 0:
            totals.append(total)
        s = timings.get("scout_seconds")
        if isinstance(s, (int, float)) and s > 0:
            scout_times.append(float(s))
        c = timings.get("clarify_seconds")
        if isinstance(c, (int, float)) and c > 0:
            clarify_times.append(float(c))
        for entry in timings.get("subtask_seconds") or []:
            # JSONL stores [task_type, seconds] pairs.
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            tt, secs = entry
            if isinstance(tt, str) and isinstance(secs, (int, float)):
                by_task.setdefault(tt, []).append(float(secs))

    task_stats: list[TaskTypeStat] = []
    for task_type, samples in by_task.items():
        if len(samples) < min_samples:
            continue
        task_stats.append(
            TaskTypeStat(
                task_type=task_type,
                samples=len(samples),
                median_seconds=_median(samples),
                p90_seconds=_percentile(samples, 90.0),
            )
        )
    # Slow first so the user sees problem children at the top.
    task_stats.sort(key=lambda s: (-s.median_seconds, s.task_type))

    phases: list[PhaseStat] = []
    if totals:
        phases.append(PhaseStat("total", len(totals), _median(totals)))
    if scout_times:
        phases.append(PhaseStat("scout", len(scout_times), _median(scout_times)))
    if clarify_times:
        phases.append(
            PhaseStat("clarify", len(clarify_times), _median(clarify_times))
        )

    return TimingSummary(
        turn_count=turn_count,
        by_task_type=task_stats,
        phases=phases,
    )


def format_summary(summary: TimingSummary) -> list[str]:
    """Render a `TimingSummary` as a list of REPL output lines.

    Returns lines instead of joining so the caller can decide on
    rich-text formatting. Empty list when there's nothing useful
    to show (no timings collected yet).
    """
    if summary.turn_count == 0:
        return ["No timing data yet — run a few asks first."]

    lines: list[str] = [
        f"📊 timing summary over {summary.turn_count} recent ask(s):"
    ]

    if summary.phases:
        phase_parts: list[str] = []
        for p in summary.phases:
            phase_parts.append(
                f"{p.phase} median {p.median_seconds:.1f}s "
                f"(n={p.samples})"
            )
        lines.append("  phases: " + " · ".join(phase_parts))

    if summary.by_task_type:
        lines.append("  per task_type:")
        for s in summary.by_task_type:
            slow = " 🐌 slow" if s.is_slow else ""
            lines.append(
                f"    {s.task_type}: median {s.median_seconds:.1f}s, "
                f"p90 {s.p90_seconds:.1f}s (n={s.samples}){slow}"
            )
    else:
        lines.append(
            "  per task_type: (need at least "
            f"{MIN_SAMPLES} samples of any one type)"
        )

    return lines
