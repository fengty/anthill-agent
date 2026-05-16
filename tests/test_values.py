"""Open-vocabulary value dimension tests.

The mechanism contract: Anthill records whichever dimensions show up
without enforcing a closed list. These tests verify
  - name normalization survives whatever the LLM throws at us
  - the catalog grows by observation, not by config
  - judge verdicts in the new multi-dim shape get parsed end-to-end
  - the trail's per-dimension EWMA actually moves toward new scores
  - persistence round-trips the catalog + trail dim_scores intact
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.judge import parse_verdict
from anthill.core.pheromone import PheromoneTrail, Trail
from anthill.core.values import (
    DimensionCatalog,
    aggregate,
    normalize_dim,
)


# --- normalize_dim ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Correctness", "correctness"),
        ("correctness", "correctness"),
        ("Correct-ness", "correct_ness"),
        ("Citation Quality", "citation_quality"),
        ("Citation  Quality", "citation_quality"),
        ("  factual_grounding  ", "factual_grounding"),
        ("over___underscore", "over_underscore"),
        ("punct!u@a#tion$", "punctuation"),
        ("123_numeric", "123_numeric"),
        ("中文", ""),  # non-latin strips empty for now — that's intentional
    ],
)
def test_normalize_dim(raw: str, expected: str) -> None:
    assert normalize_dim(raw) == expected


# --- DimensionCatalog ------------------------------------------------------


def test_observe_registers_new_dimension() -> None:
    cat = DimensionCatalog()
    cat.observe("Correctness", score=0.8, description="does it solve the task")
    assert "correctness" in cat.dimensions
    d = cat.dimensions["correctness"]
    assert d.description == "does it solve the task"
    assert d.observations == 1
    assert d.avg_score == pytest.approx(0.8)


def test_observe_idempotent_on_name() -> None:
    """Same dimension under different casings collapses to one entry."""
    cat = DimensionCatalog()
    cat.observe("correctness", score=0.5)
    cat.observe("Correctness", score=0.9)
    cat.observe("CORRECTNESS", score=0.7)
    assert list(cat.dimensions.keys()) == ["correctness"]
    assert cat.dimensions["correctness"].observations == 3


def test_observe_does_not_overwrite_existing_description() -> None:
    cat = DimensionCatalog()
    cat.observe("style", score=0.5, description="formal register")
    cat.observe("style", score=0.7, description="something else")  # later
    assert cat.dimensions["style"].description == "formal register"


def test_observe_fills_late_description() -> None:
    cat = DimensionCatalog()
    cat.observe("style", score=0.5)  # no description
    cat.observe("style", score=0.6, description="formal register")
    assert cat.dimensions["style"].description == "formal register"


def test_observe_clamps_score_to_unit_interval() -> None:
    cat = DimensionCatalog()
    cat.observe("x", score=1.5)
    cat.observe("x", score=-0.3)
    # avg stays in [0, 1] regardless of inputs
    assert 0.0 <= cat.dimensions["x"].avg_score <= 1.0


def test_observe_empty_name_raises() -> None:
    cat = DimensionCatalog()
    with pytest.raises(ValueError, match="normalizes to empty"):
        cat.observe("!!!")


def test_weight_default_and_override() -> None:
    cat = DimensionCatalog()
    assert cat.weight("correctness") == 1.0
    cat.set_weight("Correctness", 2.5)
    assert cat.weight("correctness") == 2.5
    cat.reset_weights()
    assert cat.weight("correctness") == 1.0


def test_known_lists_canonical_names_sorted() -> None:
    cat = DimensionCatalog()
    cat.observe("Tone", score=0.5)
    cat.observe("correctness", score=0.5)
    cat.observe("depth", score=0.5)
    assert cat.known() == ["correctness", "depth", "tone"]


def test_round_trip_through_dict() -> None:
    cat = DimensionCatalog()
    cat.observe("correctness", score=0.8, description="does the work")
    cat.observe("style", score=0.5)
    cat.set_weight("correctness", 2.0)
    data = cat.to_dict()
    restored = DimensionCatalog.from_dict(data)
    assert restored.dimensions["correctness"].avg_score == pytest.approx(0.8)
    assert restored.dimensions["correctness"].description == "does the work"
    assert restored.weight("correctness") == 2.0
    assert restored.weight("style") == 1.0


def test_from_dict_tolerates_garbage_weights() -> None:
    """Hand-edited values.json with a bad weight shouldn't crash load."""
    bad = {
        "dimensions": {},
        "weights": {"correctness": "not_a_number", "depth": 1.5},
    }
    cat = DimensionCatalog.from_dict(bad)
    assert cat.weight("correctness") == 1.0  # fell back to default
    assert cat.weight("depth") == 1.5


# --- aggregate -------------------------------------------------------------


def test_aggregate_empty_returns_zero() -> None:
    assert aggregate({}) == 0.0


def test_aggregate_unweighted_is_mean() -> None:
    assert aggregate({"a": 0.4, "b": 0.6}) == pytest.approx(0.5)


def test_aggregate_with_catalog_honors_weights() -> None:
    cat = DimensionCatalog()
    cat.set_weight("a", 3.0)
    cat.set_weight("b", 1.0)
    # weighted = (3*0.4 + 1*0.8) / 4 = 0.5
    assert aggregate({"a": 0.4, "b": 0.8}, cat) == pytest.approx(0.5)


def test_aggregate_falls_back_when_all_weights_zero() -> None:
    cat = DimensionCatalog()
    cat.set_weight("a", 0.0)
    cat.set_weight("b", 0.0)
    assert aggregate({"a": 0.4, "b": 0.8}, cat) == pytest.approx(0.6)


# --- judge parse_verdict (multi-dim) --------------------------------------


def test_parse_verdict_multidim_shape() -> None:
    text = """
    {
      "overall": 0.8,
      "scores": {"correctness": 0.9, "Conciseness": 0.6},
      "explanations": {"correctness": "solves it", "Conciseness": "a bit wordy"},
      "reason": "good but verbose"
    }
    """
    v = parse_verdict(text)
    assert v.score == pytest.approx(0.8)
    assert v.scores == {"correctness": 0.9, "conciseness": 0.6}
    assert v.explanations["conciseness"].startswith("a bit wordy")


def test_parse_verdict_legacy_single_score_still_works() -> None:
    """Older judges that return {'score': x, 'reason': ...} stay compatible."""
    v = parse_verdict('{"score": 0.7, "reason": "ok"}')
    assert v.score == pytest.approx(0.7)
    assert v.scores == {}
    assert v.reason == "ok"


def test_parse_verdict_overall_missing_uses_mean_of_scores() -> None:
    text = '{"scores": {"correctness": 0.6, "depth": 0.4}, "reason": "x"}'
    v = parse_verdict(text)
    assert v.score == pytest.approx(0.5)
    assert v.scores == {"correctness": 0.6, "depth": 0.4}


def test_parse_verdict_garbage_falls_back_neutral() -> None:
    v = parse_verdict("I'm not going to answer in JSON, sorry.")
    assert v.score == 0.5
    assert v.scores == {}


def test_parse_verdict_clamps_out_of_range_scores() -> None:
    v = parse_verdict('{"overall": 1.7, "scores": {"x": -0.3}}')
    assert v.score == 1.0  # clamped to upper
    assert v.scores["x"] == 0.0  # clamped to lower


def test_parse_verdict_drops_non_numeric_dim_scores() -> None:
    """A dimension with a non-numeric score is silently dropped, not retained."""
    v = parse_verdict(
        '{"overall": 0.5, "scores": {"correctness": 0.9, "tone": "ok"}}'
    )
    assert v.scores == {"correctness": 0.9}


def test_parse_verdict_normalizes_dim_keys() -> None:
    """Whatever casing the LLM used, normalized for trail consistency."""
    v = parse_verdict(
        '{"overall": 0.7, "scores": {"Citation Quality": 0.8, "FACTUAL-GROUNDING": 0.6}}'
    )
    assert "citation_quality" in v.scores
    assert "factual_grounding" in v.scores


def test_parse_verdict_handles_code_fence() -> None:
    v = parse_verdict('```json\n{"overall": 0.6, "reason": "fine"}\n```')
    assert v.score == pytest.approx(0.6)


def test_parse_verdict_extracts_embedded_json() -> None:
    """LLM wraps JSON in prose — we should still parse it."""
    text = 'Sure! {"overall": 0.7, "reason": "ok"} Hope this helps.'
    v = parse_verdict(text)
    assert v.score == pytest.approx(0.7)


# --- Trail.update_dim ------------------------------------------------------


def test_trail_update_dim_creates_entry_on_first_observation() -> None:
    t = Trail(agent_id="a", task_type="x")
    t.update_dim("correctness", 0.8)
    assert t.dim_scores["correctness"] == pytest.approx(0.8)


def test_trail_update_dim_ewma_moves_toward_new_score() -> None:
    t = Trail(agent_id="a", task_type="x")
    t.update_dim("correctness", 0.0)
    t.update_dim("correctness", 1.0)
    # alpha=0.3 ⇒ prev 0.0 → 0.0*0.7 + 1.0*0.3 = 0.3
    assert t.dim_scores["correctness"] == pytest.approx(0.3)


def test_trail_update_dim_clamps_input() -> None:
    t = Trail(agent_id="a", task_type="x")
    t.update_dim("x", 1.7)
    assert t.dim_scores["x"] == 1.0
    t.update_dim("x", -0.5)
    # 1.0 * 0.7 + 0.0 * 0.3 = 0.7
    assert t.dim_scores["x"] == pytest.approx(0.7)


# --- PheromoneTrail.record_dimensions -------------------------------------


def test_record_dimensions_creates_trail_when_absent() -> None:
    p = PheromoneTrail()
    p.record_dimensions("ant-1", "research", {"correctness": 0.8})
    trail = p._trails[("ant-1", "research")]
    # strength stays zero because record_dimensions is independent of deposit()
    assert trail.strength == 0.0
    assert trail.dim_scores["correctness"] == pytest.approx(0.8)


def test_record_dimensions_empty_dict_is_noop() -> None:
    p = PheromoneTrail()
    p.record_dimensions("ant-1", "research", {})
    assert ("ant-1", "research") not in p._trails


def test_record_dimensions_blends_with_existing_trail() -> None:
    p = PheromoneTrail()
    p.deposit("ant-1", "research", success_score=1.0)
    p.record_dimensions("ant-1", "research", {"correctness": 0.9})
    trail = p._trails[("ant-1", "research")]
    assert trail.strength > 0  # preserved
    assert trail.dim_scores["correctness"] == pytest.approx(0.9)


# --- Persistence round-trip -----------------------------------------------


def test_save_load_preserves_catalog_and_dim_scores(tmp_path: Path) -> None:
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.persistence import load_nation, save_nation

    n = Nation(name="testnat")
    n.agents = [Agent(id="ant-1", model="x")]
    n.pheromones.deposit("ant-1", "research", success_score=1.0)
    n.pheromones.record_dimensions(
        "ant-1", "research", {"correctness": 0.85, "depth": 0.6}
    )
    n.dimension_catalog.observe(
        "correctness", score=0.85, description="does the work"
    )
    n.dimension_catalog.observe("depth", score=0.6)
    n.dimension_catalog.set_weight("correctness", 2.0)

    save_nation(n, tmp_path)
    reloaded = load_nation("testnat", tmp_path)
    assert reloaded is not None

    # catalog
    assert "correctness" in reloaded.dimension_catalog.dimensions
    assert reloaded.dimension_catalog.dimensions["correctness"].description == "does the work"
    assert reloaded.dimension_catalog.weight("correctness") == 2.0

    # trail dim_scores
    trail = reloaded.pheromones._trails[("ant-1", "research")]
    assert trail.dim_scores["correctness"] == pytest.approx(0.85)
    assert trail.dim_scores["depth"] == pytest.approx(0.6)


def test_load_nation_tolerates_legacy_files_without_values_json(tmp_path: Path) -> None:
    """Pre-v0.4 nations have no values.json — load should give an empty catalog."""
    (tmp_path / "nations" / "legacy").mkdir(parents=True)
    (tmp_path / "nations" / "legacy" / "agents.json").write_text("[]")
    (tmp_path / "nations" / "legacy" / "pheromones.json").write_text("[]")
    # no values.json on purpose

    from anthill.core.persistence import load_nation
    nat = load_nation("legacy", tmp_path)
    assert nat is not None
    assert nat.dimension_catalog.known() == []


def test_load_nation_tolerates_legacy_trail_without_dim_scores(tmp_path: Path) -> None:
    """Old pheromones.json entries had no dim_scores field — should load as empty dict."""
    import json
    (tmp_path / "nations" / "legacy").mkdir(parents=True)
    (tmp_path / "nations" / "legacy" / "agents.json").write_text("[]")
    legacy_trails = [{
        "agent_id": "ant-1",
        "task_type": "research",
        "strength": 5.0,
        "alarm": 0.0,
        "last_updated": 1700000000.0,
    }]
    (tmp_path / "nations" / "legacy" / "pheromones.json").write_text(
        json.dumps(legacy_trails)
    )

    from anthill.core.persistence import load_nation
    nat = load_nation("legacy", tmp_path)
    assert nat is not None
    trail = nat.pheromones._trails[("ant-1", "research")]
    assert trail.strength == 5.0
    assert trail.dim_scores == {}
