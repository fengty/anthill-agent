"""v0.4.1 — router actually uses the open-vocabulary dimensions.

v0.4.0 stored per-dimension scores but never read them. These tests
verify the closure: if the user weights a dimension up, citizens that
score high on that dimension actually get routed to more often.
"""

from __future__ import annotations

import pytest

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.pheromone import (
    PheromoneTrail,
    dimension_modifier,
)
from anthill.core.router import Router, RouterConfig


# --- dimension_modifier (the math itself) ---------------------------------


def test_modifier_unset_weights_returns_one() -> None:
    assert dimension_modifier({"correctness": 0.9}, {}) == 1.0


def test_modifier_missing_dim_in_trail_returns_one() -> None:
    """User wants conciseness but trail only knows correctness — neutral, not penalty."""
    assert dimension_modifier({"correctness": 0.9}, {"conciseness": 2.0}) == 1.0


def test_modifier_neutral_dim_is_one() -> None:
    """Score of exactly 0.5 means 'average' — no nudge in either direction."""
    assert dimension_modifier(
        {"correctness": 0.5}, {"correctness": 2.0}
    ) == pytest.approx(1.0)


def test_modifier_high_score_with_positive_weight_boosts() -> None:
    # (1.0 * (0.9 - 0.5)) / 1.0 = 0.4 → modifier = 1.4
    assert dimension_modifier(
        {"correctness": 0.9}, {"correctness": 1.0}
    ) == pytest.approx(1.4)


def test_modifier_low_score_with_positive_weight_penalizes() -> None:
    # (1.0 * (0.1 - 0.5)) / 1.0 = -0.4 → modifier = 0.6
    assert dimension_modifier(
        {"correctness": 0.1}, {"correctness": 1.0}
    ) == pytest.approx(0.6)


def test_modifier_negative_weight_inverts() -> None:
    """Negative weight = 'less of this'; high score gets penalized."""
    # ((-1.0) * (0.9 - 0.5)) / |−1| = -0.4 → modifier = 0.6
    assert dimension_modifier(
        {"verbosity": 0.9}, {"verbosity": -1.0}
    ) == pytest.approx(0.6)


def test_modifier_clamps_to_band() -> None:
    """Even with absurd weights and perfect scores, modifier stays in [0.5, 1.5]."""
    assert dimension_modifier(
        {"x": 1.0}, {"x": 999.0}
    ) == 1.5
    assert dimension_modifier(
        {"x": 0.0}, {"x": 999.0}
    ) == 0.5


def test_modifier_combines_multiple_dimensions() -> None:
    # weights {a: 2, b: 1}, scores {a: 0.8, b: 0.2}
    # deviation = (2*(0.8-0.5) + 1*(0.2-0.5)) / (2+1) = (0.6 - 0.3) / 3 = 0.1
    assert dimension_modifier(
        {"a": 0.8, "b": 0.2}, {"a": 2.0, "b": 1.0}
    ) == pytest.approx(1.1)


def test_modifier_zero_weight_dimension_ignored() -> None:
    # weight=0 for x should not pull modifier toward neutral; only b counts
    # (1.0 * (0.9-0.5)) / 1.0 = 0.4
    assert dimension_modifier(
        {"x": 1.0, "b": 0.9}, {"x": 0.0, "b": 1.0}
    ) == pytest.approx(1.4)


# --- PheromoneTrail.ranking with dim_weights ------------------------------


def test_ranking_without_weights_matches_legacy_behavior() -> None:
    """Empty dim_weights ⇒ identical ORDER as the legacy call (scores can
    differ by decay between successive calls, but the ranking shape stays
    the same)."""
    p = PheromoneTrail()
    p.deposit("ant-1", "x", success_score=1.0)
    p.deposit("ant-2", "x", success_score=1.0)
    p.deposit("ant-2", "x", success_score=1.0)  # ant-2 has stronger trail
    legacy = [aid for aid, _ in p.ranking("x")]
    with_empty_weights = [aid for aid, _ in p.ranking("x", dim_weights={})]
    assert legacy == with_empty_weights
    assert legacy[0] == "ant-2"


def test_ranking_with_weights_promotes_dim_winner() -> None:
    """Two citizens with equal pheromone strength; one scores better on
    a weighted dimension; that one wins."""
    p = PheromoneTrail()
    p.deposit("ant-A", "translate", success_score=1.0)
    p.deposit("ant-B", "translate", success_score=1.0)
    # Both have equal base. ant-A scores high on conciseness.
    p.record_dimensions("ant-A", "translate", {"conciseness": 0.9})
    p.record_dimensions("ant-B", "translate", {"conciseness": 0.1})

    ranking = p.ranking("translate", dim_weights={"conciseness": 1.0})
    assert ranking[0][0] == "ant-A"
    assert ranking[1][0] == "ant-B"


def test_ranking_no_dim_data_is_neutral_not_penalty() -> None:
    """A citizen with no dimension scores yet should not be penalized."""
    p = PheromoneTrail()
    p.deposit("ant-newcomer", "x", success_score=1.0)
    p.deposit("ant-newcomer", "x", success_score=1.0)  # stronger base
    p.deposit("ant-veteran", "x", success_score=1.0)
    p.record_dimensions("ant-veteran", "x", {"correctness": 0.5})

    # User weights correctness up; veteran's score is just average.
    ranking = p.ranking("x", dim_weights={"correctness": 2.0})
    # newcomer has 2x deposits, no dim data → keeps full base
    # veteran has 1x deposits, neutral dim → keeps full base
    assert ranking[0][0] == "ant-newcomer"


# --- Router integration ---------------------------------------------------


def test_router_uses_nation_catalog_weights() -> None:
    """When the nation has weights set, Router.assign honors them."""
    n = Nation(name="t")
    a1 = Agent(id="ant-1", model="x")
    a2 = Agent(id="ant-2", model="x")
    n.agents = [a1, a2]
    n.pheromones.deposit("ant-1", "translate", success_score=1.0)
    n.pheromones.deposit("ant-2", "translate", success_score=1.0)
    # Equal base; ant-1 scores better on conciseness.
    n.pheromones.record_dimensions("ant-1", "translate", {"conciseness": 0.95})
    n.pheromones.record_dimensions("ant-2", "translate", {"conciseness": 0.05})

    # No weight set — random pick under exploration, but ranking-based
    # picks would be a tie. Set the weight to make ant-1 strictly better.
    n.dimension_catalog.observe("conciseness", score=0.5)
    n.dimension_catalog.set_weight("conciseness", 2.0)

    # Build a router with exploration=0 so the test is deterministic.
    router = Router(
        n.pheromones,
        n.agents,
        RouterConfig(exploration=0.0),
        dim_weights=dict(n.dimension_catalog.weights),
    )
    # Run many assigns; ant-1 should dominate.
    picks = [router.assign("translate").id for _ in range(50)]
    a1_count = sum(1 for p in picks if p == "ant-1")
    assert a1_count >= 45  # at least 90%


def test_router_without_weights_is_unbiased_by_dims() -> None:
    """No catalog weights ⇒ dim_scores have no effect on selection."""
    n = Nation(name="t")
    a1 = Agent(id="ant-1", model="x")
    a2 = Agent(id="ant-2", model="x")
    n.agents = [a1, a2]
    n.pheromones.deposit("ant-1", "x", success_score=1.0)
    n.pheromones.deposit("ant-2", "x", success_score=1.0)
    n.pheromones.deposit("ant-2", "x", success_score=1.0)  # stronger
    n.pheromones.record_dimensions("ant-1", "x", {"correctness": 1.0})
    n.pheromones.record_dimensions("ant-2", "x", {"correctness": 0.0})
    # catalog has dimensions but no weights set
    n.dimension_catalog.observe("correctness", score=0.5)

    router = Router(
        n.pheromones,
        n.agents,
        RouterConfig(exploration=0.0),
        dim_weights=dict(n.dimension_catalog.weights),
    )
    # ant-2 has stronger base; with no weights, ant-2 wins despite worse dim.
    picks = [router.assign("x").id for _ in range(20)]
    assert all(p == "ant-2" for p in picks)


def test_nation_router_property_forwards_weights() -> None:
    """The convenience Nation.router shortcut must include catalog weights."""
    n = Nation(name="t")
    n.dimension_catalog.observe("style", score=0.5)
    n.dimension_catalog.set_weight("style", 1.5)
    assert n.router.dim_weights == {"style": 1.5}


def test_router_strongest_citizen_changes_with_weights() -> None:
    """The actual behavior change v0.4.1 promises: re-weighting flips who wins."""
    n = Nation(name="t")
    a1 = Agent(id="ant-fast-shallow", model="x")
    a2 = Agent(id="ant-slow-deep", model="x")
    n.agents = [a1, a2]
    n.pheromones.deposit("ant-fast-shallow", "research", 1.0)
    n.pheromones.deposit("ant-slow-deep", "research", 1.0)
    n.pheromones.record_dimensions(
        "ant-fast-shallow", "research", {"depth": 0.2, "speed": 0.9}
    )
    n.pheromones.record_dimensions(
        "ant-slow-deep", "research", {"depth": 0.9, "speed": 0.2}
    )

    # Phase 1: prefer speed → fast wins.
    n.dimension_catalog.observe("speed", score=0.5)
    n.dimension_catalog.set_weight("speed", 1.0)
    router1 = Router(
        n.pheromones, n.agents, RouterConfig(exploration=0.0),
        dim_weights=dict(n.dimension_catalog.weights),
    )
    picks_speed = [router1.assign("research").id for _ in range(30)]
    assert picks_speed.count("ant-fast-shallow") >= 25

    # Phase 2: switch preference to depth → slow wins.
    n.dimension_catalog.reset_weights()
    n.dimension_catalog.observe("depth", score=0.5)
    n.dimension_catalog.set_weight("depth", 1.0)
    router2 = Router(
        n.pheromones, n.agents, RouterConfig(exploration=0.0),
        dim_weights=dict(n.dimension_catalog.weights),
    )
    picks_depth = [router2.assign("research").id for _ in range(30)]
    assert picks_depth.count("ant-slow-deep") >= 25
