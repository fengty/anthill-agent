"""Tests for workflow template mining."""

from __future__ import annotations

from pathlib import Path

from anthill.core.history import HistoryEntry
from anthill.core.workflows import (
    WorkflowTemplate,
    format_templates_for_scout,
    load_workflows,
    mine_workflows,
    save_workflows,
)


def _entry(plan_types: list[str], all_ok: bool = True, ts: float = 1.0) -> HistoryEntry:
    return HistoryEntry(
        id=HistoryEntry.make_id(str(plan_types), ts),
        timestamp=ts,
        request="r",
        plan=[{"task_type": t, "depends_on": []} for t in plan_types],
        outcomes=[
            {"task_type": t, "status": "ok" if all_ok else "failed", "attempts": 1, "final_output": "o", "skip_reason": None}
            for t in plan_types
        ],
    )


def test_mining_requires_recurrence() -> None:
    history = [_entry(["a", "b"])]
    assert mine_workflows(history, min_recurrence=2) == []


def test_mining_collects_repeated_shape() -> None:
    history = [
        _entry(["research", "draft"], ts=1.0),
        _entry(["research", "draft"], ts=2.0),
    ]
    templates = mine_workflows(history, min_recurrence=2)
    assert len(templates) == 1
    assert templates[0].shape == ("research", "draft")
    assert templates[0].occurrences == 2


def test_mining_filters_short_plans() -> None:
    history = [_entry(["x"], ts=1.0), _entry(["x"], ts=2.0)]
    assert mine_workflows(history, min_steps=2) == []


def test_success_rate_computed() -> None:
    history = [
        _entry(["a", "b"], all_ok=True, ts=1.0),
        _entry(["a", "b"], all_ok=True, ts=2.0),
        _entry(["a", "b"], all_ok=False, ts=3.0),
    ]
    templates = mine_workflows(history, min_recurrence=2)
    assert len(templates) == 1
    assert abs(templates[0].success_rate - 2 / 3) < 0.01


def test_sorting_by_frequency() -> None:
    history = [
        _entry(["a", "b"], ts=1.0),
        _entry(["a", "b"], ts=2.0),
        _entry(["c", "d"], ts=3.0),
        _entry(["c", "d"], ts=4.0),
        _entry(["c", "d"], ts=5.0),
    ]
    templates = mine_workflows(history, min_recurrence=2)
    assert templates[0].shape == ("c", "d")  # 3 occurrences first


def test_persistence_roundtrip(tmp_path: Path) -> None:
    templates = [
        WorkflowTemplate(shape=("a", "b"), occurrences=3, success_rate=1.0),
        WorkflowTemplate(shape=("c", "d", "e"), occurrences=2, success_rate=0.5),
    ]
    save_workflows(templates, tmp_path)
    loaded = load_workflows(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].shape == ("a", "b")
    assert loaded[1].success_rate == 0.5


def test_format_for_scout_empty_returns_empty() -> None:
    assert format_templates_for_scout([]) == ""


def test_format_for_scout_includes_signature() -> None:
    templates = [WorkflowTemplate(shape=("research", "draft"), occurrences=3, success_rate=1.0)]
    text = format_templates_for_scout(templates)
    assert "research -> draft" in text
    assert "3 runs" in text
