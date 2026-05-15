"""Tests for ask history persistence."""

from __future__ import annotations

from pathlib import Path

from anthill.core.history import (
    HistoryEntry,
    append_history,
    find_by_id,
    load_history,
    search_history,
)


def _entry(request: str, ts: float = 1.0) -> HistoryEntry:
    return HistoryEntry(
        id=HistoryEntry.make_id(request, ts),
        timestamp=ts,
        request=request,
        plan=[{"task_type": "x", "depends_on": []}],
        outcomes=[{"task_type": "x", "status": "ok", "attempts": 1, "final_output": "y", "skip_reason": None}],
    )


def test_append_and_load(tmp_path: Path) -> None:
    append_history(_entry("first request", 1.0), tmp_path)
    append_history(_entry("second request", 2.0), tmp_path)
    entries = load_history(tmp_path)
    assert len(entries) == 2
    assert entries[0].request == "first request"


def test_limit_returns_recent(tmp_path: Path) -> None:
    for i in range(5):
        append_history(_entry(f"r{i}", float(i)), tmp_path)
    last_two = load_history(tmp_path, limit=2)
    assert [e.request for e in last_two] == ["r3", "r4"]


def test_find_by_id_prefix(tmp_path: Path) -> None:
    e = _entry("hello", 1.0)
    append_history(e, tmp_path)
    found = find_by_id(e.id[:4], tmp_path)
    assert found is not None
    assert found.request == "hello"


def test_find_by_id_no_match(tmp_path: Path) -> None:
    append_history(_entry("hello", 1.0), tmp_path)
    assert find_by_id("zzzz", tmp_path) is None


def test_search_case_insensitive(tmp_path: Path) -> None:
    append_history(_entry("Translate this", 1.0), tmp_path)
    append_history(_entry("summarize the doc", 2.0), tmp_path)
    matches = search_history("TRANS", tmp_path)
    assert len(matches) == 1
    assert matches[0].request == "Translate this"


def test_id_is_stable(tmp_path: Path) -> None:
    id1 = HistoryEntry.make_id("abc", 1.0)
    id2 = HistoryEntry.make_id("abc", 1.0)
    assert id1 == id2
    id3 = HistoryEntry.make_id("abc", 2.0)
    assert id1 != id3
