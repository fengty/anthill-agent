"""Reproduction tests — fitness, mutation, lineage.

Trimmed (0.2.43) from 27 to 10 tests. Three areas covered:
  1. score_citizen / rank_citizens math
  2. reproduce() mutates the child + stamps lineage
  3. ancestors_of / descendants_of walk the chain correctly
"""

from __future__ import annotations

import random

import pytest

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.reproduction import (
    Mutation,
    ReproductionCriteria,
    ancestors_of,
    auto_reproduce,
    descendants_of,
    mutations_for_nation,
    rank_citizens,
    reproduce,
    score_citizen,
)


def _nation_with_strong_citizen() -> tuple[Nation, Agent]:
    n = Nation(name="t")
    a = Agent(model="deepseek-chat")
    n.agents = [a]
    for tt in ("research", "summarize"):
        for _ in range(5):
            n.pheromones.deposit(a.id, tt, 1.0)
    return n, a


# --- score_citizen + rank ---------------------------------------------


def test_score_citizen_qualifies_only_when_earned() -> None:
    """Two ends of the spectrum: a fresh citizen with no trails
    doesn't qualify; a citizen with strong recent trails on multiple
    task_types does. Retired citizens never qualify regardless."""
    # Unscored.
    n_empty = Nation(name="t")
    a_empty = Agent(model="x")
    n_empty.agents = [a_empty]
    s0 = score_citizen(a_empty, n_empty, ReproductionCriteria())
    assert s0.qualifies is False

    # Strong.
    n, a = _nation_with_strong_citizen()
    s1 = score_citizen(a, n, ReproductionCriteria())
    assert s1.qualifies is True
    assert s1.task_type_count == 2

    # Retired.
    a.retired_at = 1.0
    s2 = score_citizen(a, n, ReproductionCriteria())
    assert s2.qualifies is False


def test_breadth_bonus_rewards_multi_task_citizens() -> None:
    """Same total deposits, but spread across 2 task_types vs 1 →
    the multi-task citizen scores higher. Encourages generalists."""
    n = Nation(name="t")
    specialist = Agent(model="x")
    generalist = Agent(model="x")
    n.agents = [specialist, generalist]
    for _ in range(4):
        n.pheromones.deposit(specialist.id, "research", 1.0)
    for tt in ("research", "summarize"):
        for _ in range(2):
            n.pheromones.deposit(generalist.id, tt, 1.0)
    crit = ReproductionCriteria()
    assert score_citizen(generalist, n, crit).score > score_citizen(specialist, n, crit).score


def test_rank_orders_by_fitness() -> None:
    """rank_citizens returns ScoredCitizens sorted high → low."""
    n = Nation(name="t")
    fit = Agent(model="x")
    unfit = Agent(model="x")
    n.agents = [unfit, fit]
    for _ in range(5):
        n.pheromones.deposit(fit.id, "research", 1.0)
    ranked = rank_citizens(n)
    assert ranked[0].agent_id == fit.id


# --- mutations + reproduce ------------------------------------------


def test_mutations_for_nation_depends_on_model_pool() -> None:
    """Single-model nation → no model-swap mutation (nothing to swap to).
    Mixed-model nation → model-swap shows up. Both nations always
    have inherit + persona-sharpen."""
    n = Nation(name="t")
    n.agents = [Agent(model="only"), Agent(model="only")]
    names = {m.name for m in mutations_for_nation(n)}
    assert {"inherit", "persona-sharpen"}.issubset(names)
    assert "model-swap" not in names
    # Add a different model.
    n.agents.append(Agent(model="other"))
    assert "model-swap" in {m.name for m in mutations_for_nation(n)}


def test_reproduce_inherit_and_model_swap() -> None:
    """Two reproduce paths covered in one test:
      - inherit: persona unchanged, lineage stamped
      - model-swap: model swapped to a different available one
    Lineage fields (parent_id, generation) always populate."""
    n = Nation(name="t")
    n.agents = [Agent(model="alpha", persona="exact"), Agent(model="beta")]
    parent = n.agents[0]
    moves = {m.name: m for m in mutations_for_nation(n)}

    # Inherit: persona preserved.
    line_inh = reproduce(n, parent, mutation=moves["inherit"])
    assert line_inh.child.persona == "exact"
    assert line_inh.child.parent_id == parent.id
    assert line_inh.child.generation == parent.generation + 1

    # Model swap: picks the other available model.
    line_swap = reproduce(n, parent, mutation=moves["model-swap"])
    assert line_swap.child.model != parent.model


def test_reproduce_persona_sharpen_works_with_empty_persona() -> None:
    """persona-sharpen on a citizen with no persona must produce a
    valid (non-None) persona, not crash."""
    n = Nation(name="t")
    n.agents = [Agent(model="x", persona=None)]
    moves = {m.name: m for m in mutations_for_nation(n)}
    line = reproduce(n, n.agents[0], mutation=moves["persona-sharpen"])
    assert line.child.persona is not None


def test_reproduce_custom_mutation_overrides() -> None:
    """A caller-supplied Mutation runs instead of random choice — the
    primary API for explicit reproduction (CLI, tests)."""
    n = Nation(name="t")
    parent = Agent(model="x")
    n.agents = [parent]
    sentinel = []
    line = reproduce(n, parent, mutation=Mutation(
        name="custom",
        apply=lambda p, c: (sentinel.append("ran"), setattr(c, "persona", "custom"))[1],
    ))
    assert sentinel == ["ran"]
    assert line.child.persona == "custom"


# --- auto_reproduce -------------------------------------------------


def test_auto_reproduce_filters_and_caps() -> None:
    """Two contracts in one test:
      - Only qualifying citizens reproduce (no false positives).
      - max_births is respected even when many qualify."""
    n = Nation(name="t")
    parents = [Agent(model="x") for _ in range(3)]
    n.agents = list(parents)
    # Only 2 qualify (one has no trails).
    for p in parents[:2]:
        for _ in range(5):
            n.pheromones.deposit(p.id, "research", 1.0)
    # max_births=1 caps even though 2 qualify.
    lineages = auto_reproduce(n, max_births=1)
    assert len(lineages) == 1
    assert lineages[0].parent.id in {p.id for p in parents[:2]}


# --- lineage walks --------------------------------------------------


def test_ancestors_and_descendants() -> None:
    """Build a small family tree and verify both walks:
      founder → c1 → g1
              → c2
    ancestors_of(g1) = [c1, founder]
    descendants_of(founder) = {c1, c2, g1}"""
    n = Nation(name="t")
    founder = Agent(model="x")
    n.agents = [founder]
    moves = {m.name: m for m in mutations_for_nation(n)}
    c1 = reproduce(n, founder, mutation=moves["inherit"]).child
    c2 = reproduce(n, founder, mutation=moves["inherit"]).child
    g1 = reproduce(n, c1, mutation=moves["inherit"]).child

    assert [a.id for a in ancestors_of(n, g1.id)] == [c1.id, founder.id]
    assert {a.id for a in descendants_of(n, founder.id)} == {c1.id, c2.id, g1.id}
    # Founder has no ancestors; unknown id returns empty.
    assert ancestors_of(n, founder.id) == []
    assert ancestors_of(n, "ant-ghost") == []


def test_ancestors_of_breaks_on_cycle() -> None:
    """Corrupt parent_id chain (cycle) must not loop forever. The
    walk caps at a finite length and returns."""
    n = Nation(name="t")
    a = Agent(id="A", model="x", parent_id="B")
    b = Agent(id="B", model="x", parent_id="A")  # cycle
    n.agents = [a, b]
    chain = ancestors_of(n, a.id)
    assert len(chain) <= 2


def test_lineage_fields_survive_save_load(tmp_path) -> None:
    """parent_id and generation round-trip through agents.json."""
    from anthill.core.persistence import load_nation, save_nation

    n = Nation(name="t")
    founder = Agent(model="x")
    n.agents = [founder]
    moves = {m.name: m for m in mutations_for_nation(n)}
    child = reproduce(n, founder, mutation=moves["inherit"]).child
    save_nation(n, tmp_path)

    reloaded = load_nation("t", tmp_path)
    by_id = {a.id: a for a in reloaded.agents}
    assert by_id[child.id].parent_id == founder.id
    assert by_id[child.id].generation == 1
    assert by_id[founder.id].generation == 0
