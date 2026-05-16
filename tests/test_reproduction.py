"""Reproduction tests — fitness, mutation, lineage.

Three areas:
1. score_citizen / rank_citizens math against a deterministic
   pheromone state, including the breadth bonus.
2. reproduce() actually mutates the child (model and/or persona)
   and stamps lineage fields.
3. ancestors_of / descendants_of walk the parent_id chain correctly,
   including the broken-chain defenses.
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


# --- helpers --------------------------------------------------------------


def _nation_with_one_strong_citizen() -> tuple[Nation, Agent]:
    n = Nation(name="t")
    a = Agent(model="deepseek-chat")
    n.agents = [a]
    # Build strong, recent trails on two different task_types.
    for tt in ("research", "summarize"):
        for _ in range(5):
            n.pheromones.deposit(agent_id=a.id, task_type=tt, success_score=1.0)
    return n, a


# --- score_citizen --------------------------------------------------------


def test_unscored_citizen_does_not_qualify() -> None:
    n = Nation(name="t")
    a = Agent(model="x")
    n.agents = [a]
    score = score_citizen(a, n, ReproductionCriteria())
    assert score.task_type_count == 0
    assert score.qualifies is False
    assert "no pheromone trails" in score.reason()


def test_strong_citizen_qualifies() -> None:
    n, a = _nation_with_one_strong_citizen()
    score = score_citizen(a, n, ReproductionCriteria())
    assert score.task_type_count == 2
    assert score.score > 0.5
    assert score.qualifies is True
    assert "qualifies" in score.reason()


def test_breadth_bonus_rewards_multiple_task_types() -> None:
    """Two-task citizen should outscore a one-task citizen of equal raw strength."""
    n = Nation(name="t")
    specialist = Agent(model="x")
    generalist = Agent(model="x")
    n.agents = [specialist, generalist]
    # Specialist: 4 deposits on one task type.
    for _ in range(4):
        n.pheromones.deposit(specialist.id, "research", 1.0)
    # Generalist: 2 deposits on each of two task types — same total deposits.
    for tt in ("research", "summarize"):
        for _ in range(2):
            n.pheromones.deposit(generalist.id, tt, 1.0)

    crit = ReproductionCriteria()
    spec_score = score_citizen(specialist, n, crit)
    gen_score = score_citizen(generalist, n, crit)
    # Both should have positive scores; generalist gets the breadth bonus.
    assert gen_score.score > spec_score.score


def test_retired_citizen_never_qualifies() -> None:
    n, a = _nation_with_one_strong_citizen()
    a.retired_at = 1.0
    score = score_citizen(a, n, ReproductionCriteria())
    assert score.qualifies is False
    assert "retired" in score.reason()


def test_threshold_can_be_tightened() -> None:
    n, a = _nation_with_one_strong_citizen()
    crit = ReproductionCriteria(min_fitness=999.0)
    score = score_citizen(a, n, crit)
    assert score.qualifies is False


def test_rank_citizens_orders_by_fitness() -> None:
    n = Nation(name="t")
    fit = Agent(model="x")
    unfit = Agent(model="x")
    n.agents = [unfit, fit]  # deliberately reversed
    for _ in range(5):
        n.pheromones.deposit(fit.id, "research", 1.0)
    ranked = rank_citizens(n)
    assert ranked[0].agent_id == fit.id
    assert ranked[1].agent_id == unfit.id


# --- mutation -------------------------------------------------------------


def test_default_mutations_include_inherit() -> None:
    n = Nation(name="t")
    n.agents = [Agent(model="x")]
    names = {m.name for m in mutations_for_nation(n)}
    assert "inherit" in names
    assert "persona-sharpen" in names


def test_model_swap_only_offered_when_pool_has_choice() -> None:
    """Single-model nation → no model-swap mutation."""
    n = Nation(name="t")
    n.agents = [Agent(model="only"), Agent(model="only")]
    names = {m.name for m in mutations_for_nation(n)}
    assert "model-swap" not in names

    # Mixed-model nation — model-swap shows up.
    n.agents.append(Agent(model="other"))
    names2 = {m.name for m in mutations_for_nation(n)}
    assert "model-swap" in names2


def test_model_swap_picks_a_different_model() -> None:
    n = Nation(name="t")
    n.agents = [Agent(model="alpha"), Agent(model="beta")]
    parent = n.agents[0]
    # Force the model-swap mutation deterministically.
    moves = {m.name: m for m in mutations_for_nation(n)}
    lineage = reproduce(n, parent, mutation=moves["model-swap"])
    assert lineage.child.model != parent.model
    assert lineage.child.model == "beta"


def test_persona_sharpen_appends_addendum() -> None:
    n = Nation(name="t")
    n.agents = [Agent(model="x", persona="You are an architect.")]
    moves = {m.name: m for m in mutations_for_nation(n)}
    lineage = reproduce(n, n.agents[0], mutation=moves["persona-sharpen"])
    assert lineage.child.persona is not None
    assert "architect" in lineage.child.persona
    assert "concise" in lineage.child.persona


def test_persona_sharpen_handles_empty_parent_persona() -> None:
    n = Nation(name="t")
    n.agents = [Agent(model="x", persona=None)]
    moves = {m.name: m for m in mutations_for_nation(n)}
    lineage = reproduce(n, n.agents[0], mutation=moves["persona-sharpen"])
    assert lineage.child.persona is not None


# --- reproduce -----------------------------------------------------------


def test_reproduce_stamps_lineage_fields() -> None:
    n = Nation(name="t")
    parent = Agent(model="x", persona="p")
    n.agents = [parent]
    moves = {m.name: m for m in mutations_for_nation(n)}
    lineage = reproduce(n, parent, mutation=moves["inherit"])
    assert lineage.child.parent_id == parent.id
    assert lineage.child.generation == parent.generation + 1
    assert lineage.child in n.agents


def test_reproduce_inherit_keeps_persona() -> None:
    n = Nation(name="t")
    parent = Agent(model="x", persona="exact")
    n.agents = [parent]
    moves = {m.name: m for m in mutations_for_nation(n)}
    lineage = reproduce(n, parent, mutation=moves["inherit"])
    assert lineage.child.persona == "exact"
    assert "inherited unchanged" in lineage.notes


def test_reproduce_random_mutation_via_rng() -> None:
    """Passing an explicit rng makes the choice deterministic for tests."""
    n = Nation(name="t")
    n.agents = [Agent(model="alpha"), Agent(model="beta")]
    parent = n.agents[0]
    rng = random.Random(42)
    lineage = reproduce(n, parent, rng=rng)
    # We don't assert which mutation was picked — only that one ran and
    # the child is a new agent with parent_id set.
    assert lineage.child.parent_id == parent.id
    assert lineage.child in n.agents


def test_grandchild_generation_increments() -> None:
    n = Nation(name="t")
    n.agents = [Agent(model="x")]
    moves = {m.name: m for m in mutations_for_nation(n)}
    g1 = reproduce(n, n.agents[0], mutation=moves["inherit"])
    g2 = reproduce(n, g1.child, mutation=moves["inherit"])
    assert g2.child.generation == 2
    assert g2.child.parent_id == g1.child.id


# --- auto_reproduce ------------------------------------------------------


def test_auto_reproduce_only_spawns_qualifiers() -> None:
    n = Nation(name="t")
    fit = Agent(model="x")
    unfit = Agent(model="x")
    n.agents = [fit, unfit]
    for _ in range(5):
        n.pheromones.deposit(fit.id, "research", 1.0)
    # unfit has no trails ⇒ should not reproduce.

    lineages = auto_reproduce(n)
    assert len(lineages) == 1
    assert lineages[0].parent.id == fit.id


def test_auto_reproduce_caps_at_max_births() -> None:
    n = Nation(name="t")
    parents = [Agent(model="x") for _ in range(3)]
    n.agents = parents
    for p in parents:
        for _ in range(5):
            n.pheromones.deposit(p.id, "x", 1.0)

    lineages = auto_reproduce(n, max_births=2)
    assert len(lineages) == 2
    assert len(n.agents) == 5  # 3 parents + 2 children


def test_auto_reproduce_empty_when_nothing_qualifies() -> None:
    n = Nation(name="t")
    n.agents = [Agent(model="x")]  # no trails
    assert auto_reproduce(n) == []


# --- lineage walks -------------------------------------------------------


def test_ancestors_of_walks_back_to_founder() -> None:
    n = Nation(name="t")
    founder = Agent(model="x")
    n.agents = [founder]
    moves = {m.name: m for m in mutations_for_nation(n)}

    g1 = reproduce(n, founder, mutation=moves["inherit"]).child
    g2 = reproduce(n, g1, mutation=moves["inherit"]).child
    g3 = reproduce(n, g2, mutation=moves["inherit"]).child

    ancestors = ancestors_of(n, g3.id)
    assert [a.id for a in ancestors] == [g2.id, g1.id, founder.id]


def test_ancestors_of_returns_empty_for_founder() -> None:
    n = Nation(name="t")
    a = Agent(model="x")
    n.agents = [a]
    assert ancestors_of(n, a.id) == []


def test_ancestors_of_unknown_id_returns_empty() -> None:
    n = Nation(name="t")
    assert ancestors_of(n, "ant-ghost") == []


def test_descendants_of_finds_full_subtree() -> None:
    n = Nation(name="t")
    founder = Agent(model="x")
    n.agents = [founder]
    moves = {m.name: m for m in mutations_for_nation(n)}
    c1 = reproduce(n, founder, mutation=moves["inherit"]).child
    c2 = reproduce(n, founder, mutation=moves["inherit"]).child
    g1 = reproduce(n, c1, mutation=moves["inherit"]).child

    desc = descendants_of(n, founder.id)
    desc_ids = {a.id for a in desc}
    assert desc_ids == {c1.id, c2.id, g1.id}


def test_descendants_of_leaf_returns_empty() -> None:
    n = Nation(name="t")
    a = Agent(model="x")
    n.agents = [a]
    assert descendants_of(n, a.id) == []


# --- persistence integration ---------------------------------------------


def test_lineage_fields_survive_save_load(tmp_path) -> None:
    """parent_id and generation must round-trip through agents.json."""
    from anthill.core.persistence import load_nation, save_nation
    n = Nation(name="testnat")
    founder = Agent(model="x")
    n.agents = [founder]
    moves = {m.name: m for m in mutations_for_nation(n)}
    child = reproduce(n, founder, mutation=moves["inherit"]).child
    save_nation(n, tmp_path)

    reloaded = load_nation("testnat", tmp_path)
    assert reloaded is not None
    by_id = {a.id: a for a in reloaded.agents}
    assert by_id[child.id].parent_id == founder.id
    assert by_id[child.id].generation == 1
    assert by_id[founder.id].parent_id is None
    assert by_id[founder.id].generation == 0


# --- defensive paths -----------------------------------------------------


def test_ancestors_of_breaks_on_cycle() -> None:
    """A corrupted file with a cycle shouldn't loop forever."""
    n = Nation(name="t")
    a = Agent(id="A", model="x", parent_id="B")
    b = Agent(id="B", model="x", parent_id="A")  # cycle!
    n.agents = [a, b]
    chain = ancestors_of(n, a.id)
    # Should walk at most one hop and then stop.
    assert len(chain) <= 2


def test_reproduce_with_explicit_mutation_overrides_random() -> None:
    """The mutation kwarg wins over the random pick — important for tests + CLI."""
    n = Nation(name="t")
    parent = Agent(model="x")
    n.agents = [parent]
    sentinel: list[str] = []

    def _custom(p, c):  # noqa: ANN001
        sentinel.append("ran")
        c.persona = "custom"

    lineage = reproduce(n, parent, mutation=Mutation(name="custom", apply=_custom))
    assert sentinel == ["ran"]
    assert lineage.child.persona == "custom"
    assert lineage.mutation.name == "custom"


@pytest.mark.parametrize("count", [0, 1, 5])
def test_score_citizen_handles_arbitrary_citizen_counts(count) -> None:
    """Smoke test: scoring shouldn't crash on small or empty nations."""
    n = Nation(name="t")
    n.agents = [Agent(model="x") for _ in range(count)]
    if count == 0:
        assert rank_citizens(n) == []
    else:
        scores = rank_citizens(n)
        assert len(scores) == count
