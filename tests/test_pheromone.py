"""Tests for the pheromone trail mechanism."""

from __future__ import annotations

import time

from anthill.core.pheromone import PheromoneTrail


def test_deposit_increases_strength() -> None:
    p = PheromoneTrail()
    p.deposit("ant-1", "code", success_score=1.0)
    assert p.strength("ant-1", "code") > 0


def test_repeated_deposits_strengthen_trail() -> None:
    p = PheromoneTrail()
    p.deposit("ant-1", "code")
    first = p.strength("ant-1", "code")
    p.deposit("ant-1", "code")
    second = p.strength("ant-1", "code")
    assert second > first


def test_strength_is_capped() -> None:
    p = PheromoneTrail(max_strength=5.0, deposit_amount=2.0)
    for _ in range(100):
        p.deposit("ant-1", "code")
    assert p.strength("ant-1", "code") <= 5.0


def test_failure_reduces_deposit() -> None:
    p = PheromoneTrail()
    p.deposit("ant-1", "code", success_score=1.0)
    high = p.strength("ant-1", "code")
    p.deposit("ant-2", "code", success_score=0.0)
    low = p.strength("ant-2", "code")
    assert high > low
    assert low == 0


def test_strongest_returns_top_agent() -> None:
    p = PheromoneTrail()
    p.deposit("ant-1", "code", success_score=0.5)
    p.deposit("ant-2", "code", success_score=1.0)
    p.deposit("ant-2", "code", success_score=1.0)
    strongest = p.strongest_for("code")
    assert strongest is not None
    assert strongest.agent_id == "ant-2"


def test_ranking_orders_by_strength() -> None:
    p = PheromoneTrail()
    p.deposit("ant-low", "code", success_score=0.3)
    p.deposit("ant-high", "code", success_score=1.0)
    p.deposit("ant-mid", "code", success_score=0.6)
    ranked = p.ranking("code")
    assert [a for a, _ in ranked] == ["ant-high", "ant-mid", "ant-low"]


def test_task_types_are_independent() -> None:
    p = PheromoneTrail()
    p.deposit("ant-1", "code")
    assert p.strength("ant-1", "research") == 0
    assert p.strength("ant-1", "code") > 0


def test_decay_reduces_old_trails() -> None:
    p = PheromoneTrail(decay_rate=1.0)  # aggressive decay for testability
    p.deposit("ant-1", "code")
    initial = p.strength("ant-1", "code")
    # backdate the trail to simulate hours of inactivity
    p._trails[("ant-1", "code")].last_updated = time.time() - 3600
    decayed = p.strength("ant-1", "code")
    assert decayed < initial


def test_empty_strongest_returns_none() -> None:
    p = PheromoneTrail()
    assert p.strongest_for("anything") is None
