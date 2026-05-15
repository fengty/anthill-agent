"""Tests for the feedback layer."""

from __future__ import annotations

import time
from pathlib import Path

from anthill.core.feedback import (
    AskRecord,
    apply_rating,
    load_last_ask,
    save_last_ask,
)
from anthill.core.pheromone import PheromoneTrail


def test_ask_record_roundtrip(tmp_path: Path) -> None:
    record = AskRecord(
        request="test request",
        timestamp=time.time(),
        pairs=[("ant-1", "translate"), ("ant-2", "summarize")],
    )
    save_last_ask(record, tmp_path)
    loaded = load_last_ask(tmp_path)
    assert loaded is not None
    assert loaded.request == record.request
    assert loaded.pairs == record.pairs


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_last_ask(tmp_path) is None


def test_rating_up_strengthens_trails() -> None:
    p = PheromoneTrail()
    record = AskRecord(
        request="r",
        timestamp=time.time(),
        pairs=[("ant-1", "explain"), ("ant-2", "summarize")],
    )
    touched = apply_rating("up", record, p, weight=2.0)
    assert touched == 2
    assert p.strength("ant-1", "explain") > 0
    assert p.strength("ant-2", "summarize") > 0


def test_rating_down_erodes_existing_trails() -> None:
    p = PheromoneTrail()
    # Build up to strength 3
    for _ in range(3):
        p.deposit("ant-1", "explain", 1.0)
    initial = p.strength("ant-1", "explain")
    assert initial >= 3.0 * 0.99  # decay over a few microseconds

    record = AskRecord("r", time.time(), [("ant-1", "explain")])
    apply_rating("down", record, p, weight=2.0)
    after = p.strength("ant-1", "explain")
    assert after < initial


def test_rating_down_does_not_go_below_zero() -> None:
    p = PheromoneTrail()
    p.deposit("ant-1", "explain", 1.0)
    record = AskRecord("r", time.time(), [("ant-1", "explain")])
    apply_rating("down", record, p, weight=100.0)
    assert p.strength("ant-1", "explain") == 0.0


def test_rating_up_on_unknown_trail_creates_it() -> None:
    p = PheromoneTrail()
    record = AskRecord("r", time.time(), [("brand-new-ant", "novel_task")])
    apply_rating("up", record, p, weight=2.0)
    assert p.strength("brand-new-ant", "novel_task") > 0
