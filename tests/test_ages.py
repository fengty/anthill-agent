"""Tests for the four-ages progression."""

from __future__ import annotations

from anthill.core.history import HistoryEntry
from anthill.core.nation import Nation
from anthill.core.power import compute_ages


def _ok_outcome(task_type: str = "x") -> dict:
    return {
        "task_type": task_type,
        "status": "ok",
        "attempts": 1,
        "final_output": "o",
        "skip_reason": None,
    }


def test_empty_nation_all_ages_incomplete() -> None:
    ages = compute_ages(Nation(name="n"), [], [])
    assert all(not a.completed for a in ages)


def test_founding_completes_with_first_citizen() -> None:
    nation = Nation(name="n")
    nation.spawn(count=1, model="any")
    ages = compute_ages(nation, [], [])
    by_name = {a.name: a for a in ages}
    assert by_name["Founding"].completed
    assert not by_name["Specialization"].completed


def test_specialization_requires_strong_trail() -> None:
    nation = Nation(name="n")
    nation.spawn(count=1, model="any")
    # Below threshold of 2.0
    nation.pheromones.deposit(nation.agents[0].id, "explain", 1.0)
    ages = compute_ages(nation, [], [])
    assert not next(a for a in ages if a.name == "Specialization").completed

    # Above threshold
    for _ in range(2):
        nation.pheromones.deposit(nation.agents[0].id, "explain", 1.0)
    ages = compute_ages(nation, [], [])
    assert next(a for a in ages if a.name == "Specialization").completed


def test_culture_completes_with_house_style() -> None:
    nation = Nation(name="n")
    nation.culture.house_style = "Prefer terse answers."
    ages = compute_ages(nation, [], [])
    assert next(a for a in ages if a.name == "Culture").completed


def test_culture_completes_with_five_task_types() -> None:
    nation = Nation(name="n")
    for tt in ["a", "b", "c", "d", "e"]:
        nation.culture.record(tt)
    ages = compute_ages(nation, [], [])
    assert next(a for a in ages if a.name == "Culture").completed


def test_statecraft_requires_three_step_success() -> None:
    nation = Nation(name="n")
    history = [
        HistoryEntry(
            id="x", timestamp=1.0, request="r",
            plan=[],
            outcomes=[_ok_outcome(), _ok_outcome(), _ok_outcome()],
        )
    ]
    ages = compute_ages(nation, history, [])
    assert next(a for a in ages if a.name == "Statecraft").completed


def test_partial_chain_does_not_advance_statecraft() -> None:
    nation = Nation(name="n")
    history = [
        HistoryEntry(
            id="x", timestamp=1.0, request="r",
            plan=[],
            outcomes=[
                _ok_outcome(),
                {"task_type": "y", "status": "failed", "attempts": 3, "final_output": None, "skip_reason": None},
                {"task_type": "z", "status": "skipped", "attempts": 0, "final_output": None, "skip_reason": "y failed"},
            ],
        )
    ]
    ages = compute_ages(nation, history, [])
    assert not next(a for a in ages if a.name == "Statecraft").completed
