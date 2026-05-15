"""Tests for the plan cache."""

from __future__ import annotations

from pathlib import Path

from anthill.core.plan_cache import (
    load_cache,
    lookup,
    normalise_request,
    plan_key,
    remember,
    save_cache,
)
from anthill.core.scout import Plan, Subtask


def _plan() -> Plan:
    return Plan(subtasks=[Subtask(task_type="x", prompt="do x", depends_on=[])])


def test_normalise_lowercases() -> None:
    assert normalise_request("HELLO World") == "hello world"


def test_normalise_strips_punctuation() -> None:
    assert normalise_request("hi, there!") == "hi there"


def test_normalise_collapses_whitespace() -> None:
    assert normalise_request("  many   spaces\t\there ") == "many spaces here"


def test_plan_key_stable_across_normalisation() -> None:
    assert plan_key("Hello, World!") == plan_key("hello world")


def test_remember_then_lookup() -> None:
    cache: dict = {}
    p = _plan()
    remember("translate hello", p, cache)
    found = lookup("translate hello", cache)
    assert found is not None
    assert found.plan is p
    assert found.hits == 1


def test_lookup_miss_returns_none() -> None:
    assert lookup("nothing here", {}) is None


def test_repeated_lookup_increments_hits() -> None:
    cache: dict = {}
    remember("r", _plan(), cache)
    lookup("r", cache)
    lookup("r", cache)
    lookup("r", cache)
    cached = lookup("r", cache)
    assert cached.hits == 4


def test_persistence_roundtrip(tmp_path: Path) -> None:
    cache: dict = {}
    remember("translate this", _plan(), cache)
    save_cache(cache, tmp_path)
    loaded = load_cache(tmp_path)
    assert "translate this" in next(iter(loaded.values())).normalised_request or len(loaded) == 1
    assert len(loaded) == 1
