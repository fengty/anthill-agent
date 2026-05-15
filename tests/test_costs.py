"""Tests for cost tracking."""

from __future__ import annotations

from pathlib import Path

from anthill.core.costs import (
    UsageRecord,
    append_usage,
    load_usage,
    price_for,
    summarise,
)


def test_known_model_has_a_price() -> None:
    in_p, out_p = price_for("deepseek-chat")
    assert in_p > 0
    assert out_p > in_p  # output always pricier than input


def test_unknown_model_falls_back() -> None:
    in_p, out_p = price_for("nonexistent-model")
    assert in_p > 0
    assert out_p > 0


def test_record_computes_cost() -> None:
    r = UsageRecord(
        timestamp=1.0, agent_id="a1", model="deepseek-chat",
        task_type="explain", input_tokens=1_000_000, output_tokens=1_000_000,
    )
    in_p, out_p = price_for("deepseek-chat")
    assert abs(r.cost_usd - (in_p + out_p)) < 1e-6


def test_persistence_roundtrip(tmp_path: Path) -> None:
    r = UsageRecord(
        timestamp=1.0, agent_id="a1", model="deepseek-chat",
        task_type="explain", input_tokens=100, output_tokens=200,
    )
    append_usage(r, tmp_path)
    append_usage(r, tmp_path)
    loaded = load_usage(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].task_type == "explain"


def test_summarise_totals() -> None:
    records = [
        UsageRecord(1.0, "a1", "deepseek-chat", "explain", 100, 200),
        UsageRecord(2.0, "a2", "minimax", "translate", 50, 100),
    ]
    report = summarise(records)
    assert report.total_input == 150
    assert report.total_output == 300
    assert "deepseek-chat" in report.by_model
    assert "minimax" in report.by_model
    assert "explain" in report.by_task_type


def test_summarise_since_filter() -> None:
    records = [
        UsageRecord(1.0, "a", "deepseek-chat", "x", 100, 100),
        UsageRecord(100.0, "a", "deepseek-chat", "y", 100, 100),
    ]
    report = summarise(records, since=50.0)
    assert report.total_input == 100  # only the second
    assert "y" in report.by_task_type
    assert "x" not in report.by_task_type
