"""v0.4.3 — cost-efficiency as an open-vocabulary dimension.

Three things to verify:
  1. The pure math: cost_efficiency() returns the right curve.
  2. The EWMA: baseline drifts toward observations.
  3. Wiring: Nation.run() records `cost` on trail + catalog and
     respects router weights so cost-sensitive routing works end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from anthill.core.agent import Agent
from anthill.core.cost_signal import (
    BASELINE_ALPHA,
    COST_DIMENSION,
    compute_cost_usd,
    cost_efficiency,
    update_baseline,
)
from anthill.core.nation import Nation


# --- pure math -------------------------------------------------------------


def test_compute_cost_usd_uses_price_card() -> None:
    # deepseek-chat: $0.27 in / $1.10 out per million tokens
    # 1000 in + 1000 out = 0.00027 + 0.00110 = 0.00137
    cost = compute_cost_usd(1000, 1000, "deepseek-chat")
    assert cost == pytest.approx(0.00137, abs=1e-7)


def test_cost_efficiency_no_baseline_returns_neutral() -> None:
    """First attempt with nothing to compare against — neutral 0.5."""
    assert cost_efficiency(0.10, None) == 0.5
    assert cost_efficiency(0.10, 0.0) == 0.5


def test_cost_efficiency_free_is_perfect() -> None:
    assert cost_efficiency(0.0, 0.01) == 1.0


def test_cost_efficiency_at_baseline_is_perfect() -> None:
    assert cost_efficiency(0.01, 0.01) == 1.0


def test_cost_efficiency_below_baseline_is_perfect() -> None:
    assert cost_efficiency(0.005, 0.01) == 1.0


def test_cost_efficiency_double_is_half() -> None:
    assert cost_efficiency(0.02, 0.01) == pytest.approx(0.5)


def test_cost_efficiency_triple_is_zero() -> None:
    assert cost_efficiency(0.03, 0.01) == 0.0


def test_cost_efficiency_well_above_clamps_zero() -> None:
    assert cost_efficiency(10.0, 0.01) == 0.0


# --- update_baseline (EWMA) -----------------------------------------------


def test_first_update_seeds_baseline() -> None:
    baselines: dict[str, float] = {}
    val = update_baseline(baselines, "research", 0.05)
    assert val == pytest.approx(0.05)
    assert baselines["research"] == pytest.approx(0.05)


def test_subsequent_updates_smooth_toward_new_value() -> None:
    baselines = {"x": 0.10}
    update_baseline(baselines, "x", 0.20)
    # alpha=0.1: new = 0.9*0.10 + 0.1*0.20 = 0.11
    assert baselines["x"] == pytest.approx(0.11)


def test_negative_cost_is_clamped_to_zero() -> None:
    baselines: dict[str, float] = {}
    update_baseline(baselines, "x", -5.0)
    assert baselines["x"] == 0.0


def test_baseline_alpha_constant_is_sane() -> None:
    """If someone tunes this, they should know it's the smoothing constant."""
    assert 0 < BASELINE_ALPHA <= 0.5


# --- Nation.run integration ------------------------------------------------


@dataclass
class _FakeResponse:
    text: str = "ok"
    input_tokens: int = 100
    output_tokens: int = 100


class _FakeProvider:
    async def complete(self, *args, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _FakeResponse()


def _make_nation_with_provider() -> Nation:
    n = Nation(name="t")
    a = Agent(id="ant-1", model="deepseek-chat")
    a._provider = _FakeProvider()  # type: ignore[assignment]
    n.agents = [a]
    n.use_judge = False  # isolate cost from judge logic
    return n


@pytest.mark.asyncio
async def test_run_records_cost_dimension_on_trail() -> None:
    n = _make_nation_with_provider()
    await n.run("research", "hello")
    # First attempt has no baseline → efficiency=0.5 (neutral) recorded
    trail = n.pheromones._trails[("ant-1", "research")]
    assert COST_DIMENSION in trail.dim_scores


@pytest.mark.asyncio
async def test_run_registers_cost_in_catalog() -> None:
    n = _make_nation_with_provider()
    await n.run("research", "hello")
    assert COST_DIMENSION in n.dimension_catalog.dimensions
    d = n.dimension_catalog.dimensions[COST_DIMENSION]
    assert "cost-efficiency" in d.description


@pytest.mark.asyncio
async def test_run_updates_cost_baseline() -> None:
    n = _make_nation_with_provider()
    await n.run("research", "hello")
    assert "research" in n.cost_baselines
    assert n.cost_baselines["research"] > 0


@pytest.mark.asyncio
async def test_cost_dim_not_in_router_until_weighted() -> None:
    """v0.4.3 default behavior: cost recorded but router unaffected."""
    from anthill.core.router import Router, RouterConfig

    n = _make_nation_with_provider()
    n.agents.append(Agent(id="ant-2", model="deepseek-chat"))
    # ant-2 is also wired to the fake provider so it can run.
    n.agents[1]._provider = _FakeProvider()  # type: ignore[assignment]
    await n.run("x", "y")
    await n.run("x", "y")  # baseline now populated; trails have cost dim

    # No weight on cost ⇒ ranking stays based purely on strength.
    router = Router(
        n.pheromones, n.agents, RouterConfig(exploration=0.0),
        dim_weights=dict(n.dimension_catalog.weights),  # empty
    )
    ranking_no_cost = [aid for aid, _ in n.pheromones.ranking("x")]
    ranking_with_router_cfg = [
        aid for aid, _ in n.pheromones.ranking(
            "x", dim_weights=router.dim_weights
        )
    ]
    assert ranking_no_cost == ranking_with_router_cfg


@pytest.mark.asyncio
async def test_cost_weight_actually_shifts_routing() -> None:
    """The closure: set weight on cost ⇒ cheap citizen rises in ranking."""
    n = Nation(name="t")
    n.use_judge = False

    cheap = Agent(id="ant-cheap", model="deepseek-chat")
    expensive = Agent(id="ant-expensive", model="deepseek-chat")
    n.agents = [cheap, expensive]

    class _CheapProvider:
        async def complete(self, *args, **kwargs):  # noqa: ANN001, ANN201, ARG002
            return _FakeResponse(input_tokens=50, output_tokens=50)

    class _ExpensiveProvider:
        async def complete(self, *args, **kwargs):  # noqa: ANN001, ANN201, ARG002
            return _FakeResponse(input_tokens=2000, output_tokens=2000)

    cheap._provider = _CheapProvider()  # type: ignore[assignment]
    expensive._provider = _ExpensiveProvider()  # type: ignore[assignment]

    # Manually pre-seed equal pheromone strength so the test isolates
    # cost as the only differentiator.
    n.pheromones.deposit("ant-cheap", "translate", 1.0)
    n.pheromones.deposit("ant-expensive", "translate", 1.0)

    # Run a few attempts with each so cost dim_scores accumulate.
    # Force assignment by manually calling each agent's execute path.
    for _ in range(3):
        # Directly invoke the cheap one
        from anthill.core.cost_signal import compute_cost_usd, cost_efficiency, update_baseline
        cheap_cost = compute_cost_usd(50, 50, "deepseek-chat")
        baseline = n.cost_baselines.get("translate")
        score = cost_efficiency(cheap_cost, baseline)
        update_baseline(n.cost_baselines, "translate", cheap_cost)
        n.pheromones.record_dimensions("ant-cheap", "translate", {COST_DIMENSION: score})

        # And the expensive one
        exp_cost = compute_cost_usd(2000, 2000, "deepseek-chat")
        baseline = n.cost_baselines.get("translate")
        score_exp = cost_efficiency(exp_cost, baseline)
        update_baseline(n.cost_baselines, "translate", exp_cost)
        n.pheromones.record_dimensions(
            "ant-expensive", "translate", {COST_DIMENSION: score_exp}
        )

    # Now user weights cost as a routing priority.
    n.dimension_catalog.observe(COST_DIMENSION, score=0.5)
    n.dimension_catalog.set_weight(COST_DIMENSION, 1.5)

    # Compare rankings: with weight, cheap should rank above expensive.
    weighted = n.pheromones.ranking("translate", dim_weights=dict(n.dimension_catalog.weights))
    assert weighted[0][0] == "ant-cheap"


# --- persistence -----------------------------------------------------------


def test_cost_baselines_round_trip(tmp_path: Path) -> None:
    from anthill.core.persistence import load_nation, save_nation

    n = Nation(name="testnat")
    n.agents = [Agent(id="ant-1", model="x")]
    n.cost_baselines["research"] = 0.012
    n.cost_baselines["summarize"] = 0.003
    save_nation(n, tmp_path)

    reloaded = load_nation("testnat", tmp_path)
    assert reloaded is not None
    assert reloaded.cost_baselines["research"] == pytest.approx(0.012)
    assert reloaded.cost_baselines["summarize"] == pytest.approx(0.003)


def test_load_tolerates_missing_cost_baselines_file(tmp_path: Path) -> None:
    """Pre-v0.4.3 nations have no cost_baselines.json; should still load."""
    (tmp_path / "nations" / "legacy").mkdir(parents=True)
    (tmp_path / "nations" / "legacy" / "agents.json").write_text("[]")
    (tmp_path / "nations" / "legacy" / "pheromones.json").write_text("[]")

    from anthill.core.persistence import load_nation
    nat = load_nation("legacy", tmp_path)
    assert nat is not None
    assert nat.cost_baselines == {}


def test_load_tolerates_corrupt_cost_baselines(tmp_path: Path) -> None:
    """Garbled file: don't crash, just start fresh."""
    (tmp_path / "nations" / "broken").mkdir(parents=True)
    (tmp_path / "nations" / "broken" / "agents.json").write_text("[]")
    (tmp_path / "nations" / "broken" / "pheromones.json").write_text("[]")
    (tmp_path / "nations" / "broken" / "cost_baselines.json").write_text(
        "{this is not valid"
    )
    from anthill.core.persistence import load_nation
    nat = load_nation("broken", tmp_path)
    assert nat is not None
    assert nat.cost_baselines == {}
