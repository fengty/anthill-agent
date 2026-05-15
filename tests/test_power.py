"""Tests for the national strength metric."""

from __future__ import annotations

from anthill.core.feedback import Exemplar
from anthill.core.history import HistoryEntry
from anthill.core.nation import Nation
from anthill.core.power import compute_power, _normalized_entropy


def test_empty_nation_has_zero_power() -> None:
    nation = Nation(name="empty")
    report = compute_power(nation, [], [])
    assert report.overall == 0.0
    assert report.vocabulary == 0
    assert report.specialists == 0
    assert report.success_rate == 0.0


def test_vocabulary_counts_unique_task_types() -> None:
    nation = Nation(name="n")
    nation.culture.record("translate")
    nation.culture.record("translate")
    nation.culture.record("summarize")
    report = compute_power(nation, [], [])
    assert report.vocabulary == 2


def test_specialists_require_threshold_strength() -> None:
    nation = Nation(name="n")
    nation.spawn(count=2, model="any")
    a1, a2 = nation.agents
    # a1 gets deposits below threshold; a2 gets enough to count as specialist
    nation.pheromones.deposit(a1.id, "explain", 1.0)  # strength 1.0
    for _ in range(3):
        nation.pheromones.deposit(a2.id, "explain", 1.0)  # 3.0
    report = compute_power(nation, [], [], strong_trail_threshold=2.0)
    assert report.specialists == 1


def test_success_rate_from_history() -> None:
    nation = Nation(name="n")
    history = [
        HistoryEntry(
            id="x", timestamp=1.0, request="r",
            plan=[],
            outcomes=[
                {"task_type": "a", "status": "ok", "attempts": 1, "final_output": "o", "skip_reason": None},
                {"task_type": "b", "status": "failed", "attempts": 3, "final_output": None, "skip_reason": None},
            ],
        )
    ]
    report = compute_power(nation, history, [])
    assert report.success_rate == 0.5
    assert report.total_tasks == 2


def test_max_chain_only_counts_fully_successful_chains() -> None:
    nation = Nation(name="n")
    history = [
        HistoryEntry(
            id="a", timestamp=1.0, request="r1",
            plan=[],
            outcomes=[
                {"task_type": "x", "status": "ok", "attempts": 1, "final_output": "o", "skip_reason": None},
                {"task_type": "y", "status": "failed", "attempts": 3, "final_output": None, "skip_reason": None},
            ],
        ),
        HistoryEntry(
            id="b", timestamp=2.0, request="r2",
            plan=[],
            outcomes=[
                {"task_type": "x", "status": "ok", "attempts": 1, "final_output": "o", "skip_reason": None},
                {"task_type": "y", "status": "ok", "attempts": 1, "final_output": "o", "skip_reason": None},
                {"task_type": "z", "status": "ok", "attempts": 1, "final_output": "o", "skip_reason": None},
            ],
        ),
    ]
    report = compute_power(nation, history, [])
    assert report.max_chain == 3


def test_feedback_score_signs_ratings() -> None:
    exemplars = [
        Exemplar("up", "r1", "o", 1.0),
        Exemplar("up", "r2", "o", 2.0),
        Exemplar("down", "r3", "o", 3.0),
    ]
    report = compute_power(Nation(name="n"), [], exemplars)
    assert report.feedback_score == 1


def test_diversity_normalisation() -> None:
    # Two citizens each with one trail -> max entropy = 1.0
    assert _normalized_entropy([1, 1]) == 1.0
    # One citizen does everything -> 0.0
    assert _normalized_entropy([5, 0]) < 0.01
    # Uneven split should land between 0 and 1
    val = _normalized_entropy([3, 1])
    assert 0 < val < 1


def test_overall_score_caps_at_100() -> None:
    nation = Nation(name="n")
    for tt in [f"type_{i}" for i in range(20)]:
        nation.culture.record(tt)
    report = compute_power(nation, [], [])
    assert report.overall <= 100.0
