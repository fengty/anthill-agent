"""Tests for fact distillation."""

from __future__ import annotations

from pathlib import Path

from anthill.core.facts import (
    Fact,
    derive_facts,
    read_facts,
    write_facts,
)
from anthill.core.history import HistoryEntry
from anthill.core.pheromone import PheromoneTrail


def _ok_entry(plan_types: list[str], ts: float = 1.0) -> HistoryEntry:
    return HistoryEntry(
        id=HistoryEntry.make_id(str(plan_types), ts),
        timestamp=ts,
        request="r",
        plan=[{"task_type": t, "depends_on": []} for t in plan_types],
        outcomes=[
            {"task_type": t, "status": "ok", "attempts": 1, "final_output": "o", "skip_reason": None}
            for t in plan_types
        ],
    )


def test_specialist_fact_requires_strong_trail() -> None:
    p = PheromoneTrail()
    p.deposit("ant-1", "explain", 1.0)
    # Only one success — below min_evidence default of 2 (net strength < 2)
    facts = derive_facts([], p, min_evidence=2)
    assert not any(f.category == "specialist" for f in facts)


def test_specialist_fact_emerges_with_enough_evidence() -> None:
    p = PheromoneTrail()
    for _ in range(3):
        p.deposit("ant-1", "explain", 1.0)
    facts = derive_facts([], p, min_evidence=2)
    specialist_facts = [f for f in facts if f.category == "specialist"]
    assert len(specialist_facts) >= 1
    assert "ant-1" in specialist_facts[0].statement
    assert "explain" in specialist_facts[0].statement


def test_workflow_fact_requires_recurrence() -> None:
    history = [_ok_entry(["research", "draft"], ts=1.0)]
    facts = derive_facts(history, PheromoneTrail(), min_evidence=2)
    assert not any(f.category == "workflow" for f in facts)


def test_workflow_fact_emerges_on_repeated_chain() -> None:
    history = [
        _ok_entry(["research", "draft"], ts=1.0),
        _ok_entry(["research", "draft"], ts=2.0),
        _ok_entry(["research", "draft"], ts=3.0),
    ]
    facts = derive_facts(history, PheromoneTrail(), min_evidence=2)
    workflows = [f for f in facts if f.category == "workflow"]
    assert len(workflows) >= 1
    assert "research -> draft" in workflows[0].statement


def test_pattern_fact_includes_success_rate() -> None:
    history = [_ok_entry(["a"], ts=float(i)) for i in range(5)]
    facts = derive_facts(history, PheromoneTrail(), min_evidence=2)
    patterns = [f for f in facts if f.category == "pattern"]
    assert any("success rate" in f.statement.lower() for f in patterns)


def test_write_then_read(tmp_path: Path) -> None:
    facts = [
        Fact(statement="Hello", evidence_count=3, category="pattern"),
        Fact(statement="World", evidence_count=2, category="specialist"),
    ]
    write_facts(facts, tmp_path)
    content = read_facts(tmp_path)
    assert "Hello" in content
    assert "World" in content
    assert "Pattern" in content
    assert "Specialist" in content
