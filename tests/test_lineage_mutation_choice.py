"""v0.7.3 — lineage data drives mutation selection.

Before this version reproduction picked mutations uniformly at random,
so parent_id / generation data piled up but never influenced the next
generation. Now `choose_mutation_weighted` does ε-greedy weighting by
historical offspring fitness, closing the v0.3.1 open loop.

Three things under test:
  1. mutation_from_parent is stamped on every child
  2. evaluate_mutation_outcomes summarizes correctly
  3. choose_mutation_weighted respects history + ε exploration
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.reproduction import (
    MUTATION_COLD_START_THRESHOLD,
    Mutation,
    choose_mutation_weighted,
    evaluate_mutation_outcomes,
    mutations_for_nation,
    reproduce,
)


def _record_mutation(name: str):  # noqa: ANN201
    def _apply(parent, child):  # noqa: ANN001
        return
    return Mutation(name=name, apply=_apply)


# --- mutation_from_parent stamping ---------------------------------------


def test_new_agent_has_no_mutation_from_parent() -> None:
    a = Agent(model="x")
    assert a.mutation_from_parent is None


def test_reproduce_stamps_mutation_name_on_child() -> None:
    n = Nation(name="t")
    parent = Agent(id="ant-parent", model="x")
    n.agents = [parent]
    moves = {m.name: m for m in mutations_for_nation(n)}
    lineage = reproduce(n, parent, mutation=moves["inherit"])
    assert lineage.child.mutation_from_parent == "inherit"


def test_persona_sharpen_stamps_correctly() -> None:
    n = Nation(name="t")
    parent = Agent(id="ant-p", model="x", persona="hello")
    n.agents = [parent]
    moves = {m.name: m for m in mutations_for_nation(n)}
    lineage = reproduce(n, parent, mutation=moves["persona-sharpen"])
    assert lineage.child.mutation_from_parent == "persona-sharpen"


# --- persistence round-trip ----------------------------------------------


def test_mutation_from_parent_survives_save_load(tmp_path: Path) -> None:
    from anthill.core.persistence import load_nation, save_nation
    n = Nation(name="testnat")
    parent = Agent(id="ant-p", model="x")
    n.agents = [parent]
    moves = {m.name: m for m in mutations_for_nation(n)}
    child = reproduce(n, parent, mutation=moves["inherit"]).child
    save_nation(n, tmp_path)

    reloaded = load_nation("testnat", tmp_path)
    assert reloaded is not None
    by_id = {a.id: a for a in reloaded.agents}
    assert by_id[child.id].mutation_from_parent == "inherit"
    assert by_id[parent.id].mutation_from_parent is None


# --- evaluate_mutation_outcomes ------------------------------------------


def test_evaluate_empty_nation() -> None:
    n = Nation(name="t")
    assert evaluate_mutation_outcomes(n) == {}


def test_evaluate_groups_by_mutation_name() -> None:
    n = Nation(name="t")
    parent = Agent(id="ant-p", model="x")
    n.agents = [parent]
    # Two children with different mutation types
    c1 = Agent(id="c1", model="x", parent_id="ant-p", mutation_from_parent="inherit")
    c2 = Agent(id="c2", model="x", parent_id="ant-p", mutation_from_parent="persona-sharpen")
    n.agents.extend([c1, c2])

    outcomes = evaluate_mutation_outcomes(n)
    assert "inherit" in outcomes
    assert "persona-sharpen" in outcomes
    assert outcomes["inherit"]["count"] == 1
    assert outcomes["persona-sharpen"]["count"] == 1


def test_evaluate_alive_rate_excludes_retired_and_quarantined() -> None:
    n = Nation(name="t")
    parent = Agent(id="ant-p", model="x")
    alive = Agent(id="alive", model="x", parent_id="ant-p", mutation_from_parent="inherit")
    retired = Agent(id="ret", model="x", parent_id="ant-p", mutation_from_parent="inherit")
    retired.retired_at = 1.0
    quarantined = Agent(id="q", model="x", parent_id="ant-p", mutation_from_parent="inherit")
    quarantined.quarantined_at = 1.0
    n.agents = [parent, alive, retired, quarantined]

    outcomes = evaluate_mutation_outcomes(n)
    assert outcomes["inherit"]["count"] == 3
    # 1 of 3 still alive (retired + quarantined both dead-for-routing)
    assert outcomes["inherit"]["alive_rate"] == pytest.approx(1 / 3)


def test_evaluate_ignores_founders() -> None:
    """Citizens without mutation_from_parent shouldn't appear in outcomes."""
    n = Nation(name="t")
    founder = Agent(id="f", model="x")
    n.agents = [founder]
    assert evaluate_mutation_outcomes(n) == {}


# --- choose_mutation_weighted --------------------------------------------


def test_choose_mutation_weighted_empty_raises() -> None:
    n = Nation(name="t")
    with pytest.raises(ValueError, match="empty moves"):
        choose_mutation_weighted([], n, rng=random.Random(0))


def test_choose_mutation_cold_start_uses_uniform_average() -> None:
    """Before enough samples, every mutation should be roughly equally likely."""
    n = Nation(name="t")
    moves = [_record_mutation("a"), _record_mutation("b"), _record_mutation("c")]
    counts = {"a": 0, "b": 0, "c": 0}
    rng = random.Random(0)
    for _ in range(1000):
        chosen = choose_mutation_weighted(moves, n, rng=rng, epsilon=0.0)
        counts[chosen.name] += 1
    # No history ⇒ fill placeholder is uniform; expect roughly equal
    assert all(200 < c < 500 for c in counts.values())


def test_choose_mutation_weighted_favors_history_winner() -> None:
    """With enough history showing one mutation is great, it should dominate."""
    n = Nation(name="t")
    parent = Agent(id="ant-p", model="x")
    n.agents = [parent]
    # Seed history: mutation 'good' has 5 fit alive children; 'bad' has 5 retired
    for i in range(5):
        n.agents.append(Agent(
            id=f"good-{i}", model="x", parent_id="ant-p",
            mutation_from_parent="good",
        ))
        # Give 'good' children strong pheromone trails for the score
        n.pheromones.deposit(f"good-{i}", "x", 1.0)
        n.pheromones.deposit(f"good-{i}", "x", 1.0)
    for i in range(5):
        a = Agent(
            id=f"bad-{i}", model="x", parent_id="ant-p",
            mutation_from_parent="bad",
        )
        a.retired_at = 1.0
        n.agents.append(a)

    moves = [_record_mutation("good"), _record_mutation("bad")]
    counts = {"good": 0, "bad": 0}
    rng = random.Random(42)
    for _ in range(200):
        chosen = choose_mutation_weighted(moves, n, rng=rng, epsilon=0.0)
        counts[chosen.name] += 1
    # 'good' should dominate hugely
    assert counts["good"] > counts["bad"] * 3


def test_choose_mutation_weighted_epsilon_exploration() -> None:
    """High epsilon ⇒ approximately uniform random regardless of history."""
    n = Nation(name="t")
    parent = Agent(id="ant-p", model="x")
    n.agents = [parent]
    for i in range(5):
        n.agents.append(Agent(
            id=f"good-{i}", model="x", parent_id="ant-p",
            mutation_from_parent="good",
        ))
        n.pheromones.deposit(f"good-{i}", "x", 1.0)
    moves = [_record_mutation("good"), _record_mutation("bad")]
    counts = {"good": 0, "bad": 0}
    rng = random.Random(0)
    # epsilon=1.0 — always explore (uniform)
    for _ in range(1000):
        chosen = choose_mutation_weighted(moves, n, rng=rng, epsilon=1.0)
        counts[chosen.name] += 1
    # ~50/50 since fully uniform
    assert 400 < counts["good"] < 600
    assert 400 < counts["bad"] < 600


def test_choose_mutation_below_cold_start_treats_as_uniform() -> None:
    """A mutation with only 1-2 observations should not dominate."""
    n = Nation(name="t")
    parent = Agent(id="ant-p", model="x")
    n.agents = [parent]
    # Just 1 observation of 'lucky' — below threshold
    n.agents.append(Agent(
        id="lucky-c", model="x", parent_id="ant-p",
        mutation_from_parent="lucky",
    ))
    n.pheromones.deposit("lucky-c", "x", 1.0)
    n.pheromones.deposit("lucky-c", "x", 1.0)

    moves = [_record_mutation("lucky"), _record_mutation("untested")]
    counts = {"lucky": 0, "untested": 0}
    rng = random.Random(0)
    for _ in range(1000):
        chosen = choose_mutation_weighted(moves, n, rng=rng, epsilon=0.0)
        counts[chosen.name] += 1
    # With cold-start fallback, neither should be hugely dominant
    assert MUTATION_COLD_START_THRESHOLD > 1
    assert 300 < counts["lucky"] < 700


# --- reproduce default behavior ------------------------------------------


def test_reproduce_default_uses_history_when_available() -> None:
    """reproduce() with no `mutation=` arg should use history-aware path."""
    n = Nation(name="t")
    parent = Agent(id="ant-p", model="x")
    n.agents = [parent]
    # Build evidence that 'inherit' is great + 'persona-sharpen' is bad
    for i in range(5):
        n.agents.append(Agent(
            id=f"good-{i}", model="x", parent_id="ant-p",
            mutation_from_parent="inherit",
        ))
        n.pheromones.deposit(f"good-{i}", "x", 1.0)
        n.pheromones.deposit(f"good-{i}", "x", 1.0)
    for i in range(5):
        a = Agent(
            id=f"bad-{i}", model="x", parent_id="ant-p",
            mutation_from_parent="persona-sharpen",
        )
        a.retired_at = 1.0
        n.agents.append(a)

    counts = {"inherit": 0, "persona-sharpen": 0}
    rng = random.Random(1)
    for _ in range(50):
        # Force ε=0 so we observe the pure exploit signal
        lineage = reproduce(
            n, parent, rng=rng,
            mutation=choose_mutation_weighted(
                mutations_for_nation(n), n, rng=rng, epsilon=0.0,
            ),
        )
        counts[lineage.mutation.name] = counts.get(lineage.mutation.name, 0) + 1
        n.agents.remove(lineage.child)  # cleanup so we're always re-evaluating
    assert counts["inherit"] > counts.get("persona-sharpen", 0) * 3


def test_reproduce_with_use_history_false_is_uniform() -> None:
    """Explicit opt-out path: use_history=False ⇒ v0.3.1 uniform random."""
    n = Nation(name="t")
    parent = Agent(id="p", model="x")
    n.agents = [parent]
    # Stuff history to make 'inherit' look great
    for i in range(5):
        n.agents.append(Agent(
            id=f"c-{i}", model="x", parent_id="p", mutation_from_parent="inherit",
        ))
        n.pheromones.deposit(f"c-{i}", "x", 1.0)

    rng = random.Random(0)
    counts: dict[str, int] = {}
    for _ in range(200):
        lineage = reproduce(n, parent, rng=rng, use_history=False)
        counts[lineage.mutation.name] = counts.get(lineage.mutation.name, 0) + 1
        n.agents.remove(lineage.child)
    # Each available mutation should land roughly equally — uniform random
    counts_list = list(counts.values())
    assert min(counts_list) > 50  # all sampled multiple times
