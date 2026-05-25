"""Open-vocabulary value dimension tests.

The mechanism contract: Anthill records whichever dimensions show up
without enforcing a closed list. Trimmed (0.2.43) from 33 to 10
core tests covering:
  - name normalization (parametrized briefly)
  - catalog grows by observation + persistence round-trips
  - judge verdict parsing for both multi-dim and legacy formats
  - per-dimension EWMA in the trail
  - persistence + legacy file tolerance
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anthill.core.judge import parse_verdict
from anthill.core.pheromone import PheromoneTrail, Trail
from anthill.core.values import (
    DimensionCatalog,
    aggregate,
    normalize_dim,
)


# --- normalize_dim: the canonical transformation -----------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Citation Quality", "citation_quality"),  # case + space
        ("over___underscore", "over_underscore"),  # collapse runs
        ("punct!u@a#tion$", "punctuation"),        # strip punct
        ("中文", ""),                              # non-latin → empty (intentional)
    ],
)
def test_normalize_dim_covers_main_transforms(raw: str, expected: str) -> None:
    assert normalize_dim(raw) == expected


# --- DimensionCatalog: observe, weight, round-trip ---------------------


def test_catalog_observe_and_weight_basic() -> None:
    """Catalog grows on observe; same name (any casing) collapses;
    weights default to 1.0 and can be overridden; scores clamp."""
    cat = DimensionCatalog()
    cat.observe("Correctness", score=0.8, description="does the work")
    cat.observe("CORRECTNESS", score=1.5)  # higher casing + clamp
    assert list(cat.dimensions.keys()) == ["correctness"]
    assert cat.dimensions["correctness"].observations == 2
    assert 0 <= cat.dimensions["correctness"].avg_score <= 1.0
    # Description survives the second observation (not overwritten).
    assert cat.dimensions["correctness"].description == "does the work"
    # Weights default + override + reset.
    assert cat.weight("correctness") == 1.0
    cat.set_weight("Correctness", 2.5)
    assert cat.weight("correctness") == 2.5


def test_catalog_empty_name_raises() -> None:
    """A name that normalizes to empty (e.g. all-punct) raises —
    the model gave us garbage, surface it loudly not silently."""
    with pytest.raises(ValueError):
        DimensionCatalog().observe("!!!")


def test_catalog_round_trip_through_dict() -> None:
    """Save → load preserves dimensions + weights + descriptions."""
    cat = DimensionCatalog()
    cat.observe("correctness", score=0.8, description="does the work")
    cat.set_weight("correctness", 2.0)
    restored = DimensionCatalog.from_dict(cat.to_dict())
    assert restored.dimensions["correctness"].description == "does the work"
    assert restored.weight("correctness") == 2.0


# --- aggregate: weighted mean over dim scores --------------------------


def test_aggregate_weighted_with_fallback() -> None:
    """Combined: weighted mean honors catalog weights, AND falls
    back to plain mean when all weights are zero (avoid div-by-zero
    making the value silently disappear)."""
    cat = DimensionCatalog()
    cat.set_weight("a", 3.0)
    cat.set_weight("b", 1.0)
    # weighted = (3*0.4 + 1*0.8) / 4 = 0.5
    assert aggregate({"a": 0.4, "b": 0.8}, cat) == pytest.approx(0.5)
    # zero weights → fall back to plain mean
    cat.set_weight("a", 0.0)
    cat.set_weight("b", 0.0)
    assert aggregate({"a": 0.4, "b": 0.8}, cat) == pytest.approx(0.6)


# --- judge parse_verdict: multidim + legacy + edge cases -------------


def test_parse_verdict_multidim_normalized() -> None:
    """Multi-dim shape parses, dim keys normalize, overall+scores."""
    text = """
    {
      "overall": 0.8,
      "scores": {"Citation Quality": 0.9, "FACTUAL-GROUNDING": 0.6},
      "reason": "good"
    }
    """
    v = parse_verdict(text)
    assert v.score == pytest.approx(0.8)
    assert "citation_quality" in v.scores
    assert "factual_grounding" in v.scores


def test_parse_verdict_legacy_and_robust() -> None:
    """Three robustness behaviors in one test:
      - legacy {score, reason} shape still works
      - garbage falls back neutral (0.5) not raise
      - JSON embedded in prose with code fence still extracts
    """
    # Legacy single-score format.
    assert parse_verdict('{"score": 0.7, "reason": "ok"}').score == pytest.approx(0.7)
    # Garbage.
    assert parse_verdict("not json").score == 0.5
    # Code fence + prose around JSON.
    assert parse_verdict('```json\n{"overall": 0.6}\n```').score == pytest.approx(0.6)
    assert parse_verdict('Sure! {"overall": 0.7} cheers.').score == pytest.approx(0.7)


# --- Trail.update_dim: EWMA + clamp ----------------------------------


def test_trail_update_dim_ewma_and_clamp() -> None:
    """First observation seeds the value; subsequent ones blend via
    EWMA (alpha=0.3). Inputs outside [0,1] clamp before blending."""
    t = Trail(agent_id="a", task_type="x")
    t.update_dim("x", 0.0)
    t.update_dim("x", 1.0)
    # 0.0 * 0.7 + 1.0 * 0.3 = 0.3
    assert t.dim_scores["x"] == pytest.approx(0.3)
    # Clamp: 1.7 → 1.0, blend: 0.3 * 0.7 + 1.0 * 0.3 = 0.51
    t.update_dim("x", 1.7)
    assert t.dim_scores["x"] == pytest.approx(0.51)


def test_record_dimensions_creates_or_blends() -> None:
    """record_dimensions: creates a trail on first call (with
    strength=0), blends with existing trail without resetting
    strength. Empty dict is a no-op."""
    p = PheromoneTrail()
    # No-op on empty.
    p.record_dimensions("ant-1", "research", {})
    assert ("ant-1", "research") not in p._trails
    # Creates fresh trail.
    p.record_dimensions("ant-1", "research", {"correctness": 0.8})
    assert p._trails[("ant-1", "research")].strength == 0.0
    # Then deposit + record_dimensions: strength preserved.
    p.deposit("ant-1", "research", success_score=1.0)
    p.record_dimensions("ant-1", "research", {"correctness": 0.9})
    trail = p._trails[("ant-1", "research")]
    assert trail.strength > 0


# --- Persistence + legacy file tolerance ------------------------------


def test_persistence_round_trip(tmp_path: Path) -> None:
    """Save → load preserves catalog + trail dim_scores."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.persistence import load_nation, save_nation

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.pheromones.deposit("ant-1", "research", success_score=1.0)
    n.pheromones.record_dimensions(
        "ant-1", "research", {"correctness": 0.85}
    )
    n.dimension_catalog.observe("correctness", score=0.85)
    n.dimension_catalog.set_weight("correctness", 2.0)

    save_nation(n, tmp_path)
    reloaded = load_nation("t", tmp_path)
    assert reloaded is not None
    assert reloaded.dimension_catalog.weight("correctness") == 2.0
    assert (
        reloaded.pheromones._trails[("ant-1", "research")].dim_scores["correctness"]
        == pytest.approx(0.85)
    )


def test_persistence_tolerates_legacy_files(tmp_path: Path) -> None:
    """Pre-v0.4 nations lacked values.json AND trails lacked
    dim_scores. Both load cleanly without crashing."""
    (tmp_path / "nations" / "legacy").mkdir(parents=True)
    (tmp_path / "nations" / "legacy" / "agents.json").write_text("[]")
    (tmp_path / "nations" / "legacy" / "pheromones.json").write_text(
        json.dumps([{
            "agent_id": "ant-1", "task_type": "research",
            "strength": 5.0, "alarm": 0.0,
            "last_updated": 1700000000.0,
        }])
    )
    # No values.json on purpose.
    from anthill.core.persistence import load_nation
    nat = load_nation("legacy", tmp_path)
    assert nat is not None
    assert nat.dimension_catalog.known() == []
    assert nat.pheromones._trails[("ant-1", "research")].dim_scores == {}
