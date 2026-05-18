"""0.1.52 — aggregate per-task_type + per-phase latency stats.

Builds on the timings 0.1.44/47 started writing to session JSONL.
Verifies:
  - turns without `timings` field (pre-0.1.44 logs) are skipped
  - per-task_type median + p90 + sample count
  - task_type with < MIN_SAMPLES is filtered out (not enough data)
  - scout / clarify / total medians ignore None-phases
  - is_slow tag fires above SLOW_TASK_SECONDS threshold
  - format_summary handles empty input + "need more samples" case

Synthetic turn dicts mirror the exact JSONL shape AskTimings.to_dict
emits, so any breakage from a schema change would surface here.
"""

from __future__ import annotations

from anthill.core.timing_stats import (
    MIN_SAMPLES,
    SLOW_TASK_SECONDS,
    PhaseStat,
    TaskTypeStat,
    TimingSummary,
    format_summary,
    summarize_timings,
)


# --- helpers --------------------------------------------------------------


def _turn(
    *,
    total: float = 5.0,
    scout: float | None = 1.0,
    clarify: float | None = None,
    subtasks: list[tuple[str, float]] | None = None,
) -> dict:
    return {
        "kind": "turn",
        "ts": 1.0,
        "request": "x",
        "final_output": "y",
        "duration": total,
        "timings": {
            "total_seconds": total,
            "scout_seconds": scout,
            "clarify_seconds": clarify,
            "subtask_seconds": (
                [[tt, s] for tt, s in (subtasks or [])]
            ),
            "refusal_retry_count": 0,
            "plan_source": "scout",
        },
    }


# --- summarize_timings core behavior --------------------------------------


def test_summarize_empty_input() -> None:
    s = summarize_timings([])
    assert s.turn_count == 0
    assert s.by_task_type == []
    assert s.phases == []


def test_summarize_skips_pre_044_turns_without_timings() -> None:
    turns = [
        {"kind": "turn", "ts": 1.0, "request": "old", "final_output": "y"},
        _turn(total=3.0, subtasks=[("general", 2.5)]),
    ]
    s = summarize_timings(turns)
    assert s.turn_count == 1  # only the one with timings counted


def test_summarize_filters_task_types_below_min_samples() -> None:
    # MIN_SAMPLES is 3. With 2 occurrences of "research", we don't
    # surface a stat for it — too noisy.
    turns = [
        _turn(subtasks=[("research", 5.0)]),
        _turn(subtasks=[("research", 6.0)]),
    ]
    s = summarize_timings(turns)
    assert [t.task_type for t in s.by_task_type] == []


def test_summarize_includes_task_types_at_min_samples() -> None:
    turns = [
        _turn(subtasks=[("research", 5.0)]),
        _turn(subtasks=[("research", 6.0)]),
        _turn(subtasks=[("research", 7.0)]),
    ]
    s = summarize_timings(turns, min_samples=MIN_SAMPLES)
    assert len(s.by_task_type) == 1
    assert s.by_task_type[0].task_type == "research"
    assert s.by_task_type[0].samples == 3


def test_summarize_computes_median_and_p90() -> None:
    # 5 samples of "analyze": 2, 4, 6, 8, 20. Median = 6, p90 ≈ 15.2.
    turns = [
        _turn(subtasks=[("analyze", v)]) for v in (2.0, 4.0, 6.0, 8.0, 20.0)
    ]
    s = summarize_timings(turns, min_samples=3)
    [stat] = s.by_task_type
    assert stat.median_seconds == 6.0
    # Inclusive linear-interp p90: idx = 4*0.9 = 3.6 → 8 + 0.6*(20-8) = 15.2
    assert abs(stat.p90_seconds - 15.2) < 0.01


def test_summarize_sorts_slowest_first() -> None:
    turns = (
        [_turn(subtasks=[("fast", 1.0)])] * 3
        + [_turn(subtasks=[("slow", 30.0)])] * 3
    )
    s = summarize_timings(turns)
    assert [t.task_type for t in s.by_task_type] == ["slow", "fast"]


def test_summarize_phase_stats_ignore_none() -> None:
    """clarify_seconds=None on a turn should not deflate the
    clarify median by counting as 0s. Only turns where the phase
    actually fired contribute."""
    turns = [
        _turn(scout=2.0, clarify=None),
        _turn(scout=4.0, clarify=8.0),
        _turn(scout=6.0, clarify=None),
    ]
    s = summarize_timings(turns)
    phases = {p.phase: p for p in s.phases}
    assert phases["clarify"].samples == 1  # only one turn had clarify
    assert phases["clarify"].median_seconds == 8.0
    assert phases["scout"].samples == 3
    assert phases["scout"].median_seconds == 4.0


def test_summarize_total_phase_uses_total_seconds() -> None:
    turns = [
        _turn(total=2.0, subtasks=[("a", 1.0)]),
        _turn(total=4.0, subtasks=[("a", 1.0)]),
        _turn(total=6.0, subtasks=[("a", 1.0)]),
    ]
    s = summarize_timings(turns)
    total_phase = next(p for p in s.phases if p.phase == "total")
    assert total_phase.median_seconds == 4.0


# --- is_slow tag ----------------------------------------------------------


def test_task_type_stat_is_slow_fires_above_threshold() -> None:
    s = TaskTypeStat(
        task_type="research",
        samples=10,
        median_seconds=SLOW_TASK_SECONDS + 5.0,
        p90_seconds=30.0,
    )
    assert s.is_slow is True


def test_task_type_stat_not_slow_below_threshold() -> None:
    s = TaskTypeStat(
        task_type="research",
        samples=10,
        median_seconds=SLOW_TASK_SECONDS - 1.0,
        p90_seconds=10.0,
    )
    assert s.is_slow is False


# --- format_summary -------------------------------------------------------


def test_format_summary_empty_says_no_data() -> None:
    out = format_summary(TimingSummary(0, [], []))
    assert any("no timing data" in line.lower() for line in out)


def test_format_summary_mentions_each_phase() -> None:
    summary = TimingSummary(
        turn_count=5,
        by_task_type=[],
        phases=[
            PhaseStat("total", 5, 14.8),
            PhaseStat("scout", 5, 1.5),
            PhaseStat("clarify", 2, 7.4),
        ],
    )
    out = "\n".join(format_summary(summary))
    assert "total median 14.8s" in out
    assert "scout median 1.5s" in out
    assert "clarify median 7.4s" in out


def test_format_summary_tags_slow_task_types() -> None:
    summary = TimingSummary(
        turn_count=5,
        by_task_type=[
            TaskTypeStat("research", 4, SLOW_TASK_SECONDS + 3.0, 30.0),
            TaskTypeStat("general", 5, 2.0, 4.0),
        ],
        phases=[],
    )
    out = "\n".join(format_summary(summary))
    assert "🐌 slow" in out
    # research should be tagged, general should not. Verify general
    # line doesn't have the tag.
    general_line = next(
        line for line in format_summary(summary) if "general:" in line
    )
    assert "🐌" not in general_line


def test_format_summary_explains_why_no_per_task_section() -> None:
    """When we have phases but no task_type cleared MIN_SAMPLES,
    the output explains *why* the per-task section is missing so the
    user knows to keep asking."""
    summary = TimingSummary(
        turn_count=2,
        by_task_type=[],
        phases=[PhaseStat("total", 2, 5.0)],
    )
    out = "\n".join(format_summary(summary))
    assert "samples" in out or "need" in out.lower()
