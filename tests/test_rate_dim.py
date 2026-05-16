"""v0.4.2 — anthill rate --dim closes the rate→catalog→router loop.

Two surfaces under test:
  1. apply_rating now accepts dim_scores + catalog and lands per-dim data
     on the trail AND in the catalog (so it's immediately weightable).
  2. The CLI parser for --dim takes up/down/+/-/float and normalizes keys
     the same way the judge path does.
"""

from __future__ import annotations

import pytest

from anthill.cli.main import _parse_dim_arg
from anthill.core.feedback import AskRecord, apply_rating
from anthill.core.pheromone import PheromoneTrail
from anthill.core.values import DimensionCatalog


# --- _parse_dim_arg --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("correctness=up", ("correctness", 1.0)),
        ("correctness=down", ("correctness", 0.0)),
        ("correctness=+", ("correctness", 1.0)),
        ("correctness=-", ("correctness", 0.0)),
        ("conciseness=0.3", ("conciseness", 0.3)),
        ("conciseness=1.7", ("conciseness", 1.0)),  # clamp upper
        ("conciseness=-0.5", ("conciseness", 0.0)),  # clamp lower
        ("Correct-ness=up", ("correct_ness", 1.0)),
        ("Citation Quality=0.8", ("citation_quality", 0.8)),
    ],
)
def test_parse_dim_arg_happy_path(raw: str, expected: tuple[str, float]) -> None:
    assert _parse_dim_arg(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "no_equals_sign",
        "=missing_name",
        "name=unparseable",
        "!!!=up",  # name normalizes to empty
        "name=",
    ],
)
def test_parse_dim_arg_rejects_malformed(raw: str) -> None:
    assert _parse_dim_arg(raw) is None


# --- apply_rating with dim_scores -----------------------------------------


def _record(pairs: list[tuple[str, str]]) -> AskRecord:
    return AskRecord(
        request="r", timestamp=0.0, pairs=pairs, final_output="out"
    )


def test_apply_rating_without_dims_unchanged() -> None:
    """Existing behavior must stay byte-identical when dim_scores is None."""
    p = PheromoneTrail()
    rec = _record([("ant-1", "translate")])
    touched = apply_rating("up", rec, p, weight=2.0)
    assert touched == 1
    trail = p._trails[("ant-1", "translate")]
    assert trail.strength > 0
    assert trail.dim_scores == {}


def test_apply_rating_with_dims_records_to_trail() -> None:
    p = PheromoneTrail()
    rec = _record([("ant-1", "translate")])
    apply_rating(
        "up", rec, p, weight=2.0,
        dim_scores={"correctness": 0.9, "conciseness": 0.3},
    )
    trail = p._trails[("ant-1", "translate")]
    assert trail.dim_scores["correctness"] == pytest.approx(0.9)
    assert trail.dim_scores["conciseness"] == pytest.approx(0.3)


def test_apply_rating_with_catalog_registers_new_dims() -> None:
    """User's --dim names should auto-register the same way judge's do."""
    p = PheromoneTrail()
    cat = DimensionCatalog()
    rec = _record([("ant-1", "x")])
    apply_rating(
        "up", rec, p, weight=2.0,
        dim_scores={"citation_quality": 0.8},
        catalog=cat,
    )
    assert "citation_quality" in cat.dimensions
    assert cat.dimensions["citation_quality"].avg_score == pytest.approx(0.8)


def test_apply_rating_down_with_dims_orthogonal() -> None:
    """`rate down --dim correctness=up` should erode strength but record the dim."""
    p = PheromoneTrail()
    p.deposit("ant-1", "x", success_score=1.0)
    p.deposit("ant-1", "x", success_score=1.0)
    before = p._trails[("ant-1", "x")].strength
    rec = _record([("ant-1", "x")])
    apply_rating(
        "down", rec, p, weight=2.0,
        dim_scores={"correctness": 0.9},  # the one thing they did right
    )
    trail = p._trails[("ant-1", "x")]
    assert trail.strength < before  # eroded by down rating
    assert trail.dim_scores["correctness"] == pytest.approx(0.9)


def test_apply_rating_dims_applied_to_every_pair() -> None:
    p = PheromoneTrail()
    rec = _record([("ant-1", "research"), ("ant-2", "summarize")])
    apply_rating(
        "up", rec, p, weight=2.0,
        dim_scores={"correctness": 0.85},
    )
    assert p._trails[("ant-1", "research")].dim_scores["correctness"] == pytest.approx(0.85)
    assert p._trails[("ant-2", "summarize")].dim_scores["correctness"] == pytest.approx(0.85)


def test_full_closure_rate_dim_then_route() -> None:
    """End-to-end: user rates a dimension, then weights it, then router shifts."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.router import Router, RouterConfig

    n = Nation(name="t")
    n.agents = [
        Agent(id="ant-good", model="x"),
        Agent(id="ant-bad", model="x"),
    ]
    n.pheromones.deposit("ant-good", "translate", 1.0)
    n.pheromones.deposit("ant-bad", "translate", 1.0)

    # User rates the good one up on conciseness via the dimension path.
    apply_rating(
        "up",
        _record([("ant-good", "translate")]),
        n.pheromones,
        weight=0.5,  # small impact on strength
        dim_scores={"conciseness": 1.0},
        catalog=n.dimension_catalog,
    )
    # And the bad one down on the same dim.
    apply_rating(
        "down",
        _record([("ant-bad", "translate")]),
        n.pheromones,
        weight=0.5,
        dim_scores={"conciseness": 0.0},
        catalog=n.dimension_catalog,
    )

    # Activate conciseness as a router preference.
    n.dimension_catalog.set_weight("conciseness", 2.0)

    router = Router(
        n.pheromones, n.agents, RouterConfig(exploration=0.0),
        dim_weights=dict(n.dimension_catalog.weights),
    )
    picks = [router.assign("translate").id for _ in range(50)]
    # Good citizen should now dominate because conciseness is weighted up
    # AND user's dim feedback registered ant-good high, ant-bad low.
    assert picks.count("ant-good") >= 45
