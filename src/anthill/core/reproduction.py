"""Reproduction — high-reputation citizens spawn variation-mutated offspring.

v0.3.0 made citizens mortal. This is the other half of natural
selection: someone has to be born to replace what dies. But "spawn N
new citizens" was already a thing. What's new in v0.3.1 is *which*
citizens get to reproduce and *how* the child differs from the
parent — the two ingredients that turn spawning into evolution.

Fitness:
  A citizen earns the right to reproduce when its accumulated work
  has built strong, recently-active pheromone trails. The fitness
  score is the sum of its trail strengths after decay (which is what
  the pheromone module already reports through `ranking`), with a
  small bonus for breadth — citizens that have proved themselves on
  more than one task type are valued slightly more than one-trick
  specialists. The exact formula is intentionally simple; we want
  the user to see a number they can reason about, not a black box.

Variation:
  A child differs from its parent in one of three ways: model swap,
  persona tweak, or both. The variation is sampled from a small set
  of moves (see DEFAULT_MUTATIONS) so the user can predict what
  kinds of changes the next generation will explore. Aggressive
  mutation is left to user code; the defaults stay close to the
  parent so a nation doesn't churn its identity in one round.

Reproduction:
  The new child is added to nation.agents with parent_id and
  generation set, and the user is told what changed. The parent
  isn't touched — reproduction is additive, not replacement. Pair
  reproduction with retirement (v0.3.0) to get the full cycle: old
  dies, fit reproduces, the nation evolves.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation


# Defaults chosen so a single-citizen nation can reproduce on day one
# without needing a long history. A user who wants stricter selection
# raises min_fitness.
DEFAULT_MIN_FITNESS = 0.5
DEFAULT_MIN_TASK_TYPES = 1


@dataclass
class FitnessScore:
    """Why this citizen is or isn't a reproduction candidate."""

    agent_id: str
    model: str
    is_retired: bool
    total_strength: float       # sum of decayed trail strengths
    task_type_count: int        # distinct task_types with any trail
    score: float                # the combined fitness number
    qualifies: bool             # met the criteria

    def reason(self) -> str:
        """One-line explanation for why this citizen was or wasn't picked."""
        if self.is_retired:
            return "retired"
        if self.task_type_count == 0:
            return "no pheromone trails yet"
        if not self.qualifies:
            return f"fitness {self.score:.2f} below threshold"
        return f"qualifies (fitness {self.score:.2f}, {self.task_type_count} trail(s))"


@dataclass
class ReproductionCriteria:
    """Thresholds for who may reproduce. Explicit knobs over magic numbers."""

    min_fitness: float = DEFAULT_MIN_FITNESS
    min_task_types: int = DEFAULT_MIN_TASK_TYPES
    # Diversity bonus: each extra distinct task_type beyond the first
    # adds this much to the score. Keeps specialists viable while
    # rewarding citizens that handle multiple kinds of work.
    breadth_bonus_per_type: float = 0.1


@dataclass
class Mutation:
    """One named change a child can carry relative to its parent."""

    name: str
    apply: Callable[["Agent", "Agent"], None]  # mutates the child in place


@dataclass
class Lineage:
    """The child + a record of what changed during reproduction."""

    child: "Agent"
    parent: "Agent"
    mutation: Mutation
    notes: list[str] = field(default_factory=list)


# --- fitness --------------------------------------------------------------


def score_citizen(
    agent: "Agent",
    nation: "Nation",
    criteria: ReproductionCriteria,
) -> FitnessScore:
    """Compute one citizen's fitness without mutating anything."""
    total = 0.0
    task_types: set[str] = set()
    # nation.pheromones.trails() applies decay on read — we want the
    # post-decay reading so a dormant citizen scores accordingly.
    for trail in nation.pheromones.trails():
        if trail.agent_id != agent.id:
            continue
        # Use strength minus alarm (the same view the router takes).
        net = max(0.0, trail.strength - trail.alarm)
        total += net
        if net > 0:
            task_types.add(trail.task_type)

    breadth = max(0, len(task_types) - 1) * criteria.breadth_bonus_per_type
    score = total + breadth
    qualifies = (
        not agent.is_retired
        and len(task_types) >= criteria.min_task_types
        and score >= criteria.min_fitness
    )
    return FitnessScore(
        agent_id=agent.id,
        model=agent.model,
        is_retired=agent.is_retired,
        total_strength=total,
        task_type_count=len(task_types),
        score=score,
        qualifies=qualifies,
    )


def rank_citizens(
    nation: "Nation",
    criteria: ReproductionCriteria | None = None,
) -> list[FitnessScore]:
    """Score every citizen, sorted by fitness descending."""
    crit = criteria or ReproductionCriteria()
    scores = [score_citizen(a, nation, crit) for a in nation.agents]
    scores.sort(key=lambda s: s.score, reverse=True)
    return scores


# --- variation ------------------------------------------------------------


def _persona_tweak(parent: "Agent", child: "Agent") -> None:
    """Append a small, deterministic-feeling refinement to the persona.

    Models tend to read system prompts literally. Appending a short
    sharper-edge phrase is a low-risk way to nudge behavior without
    rewriting the parent's persona entirely.
    """
    base = (parent.persona or "").strip()
    addendum = " Prefer concise answers grounded in the prompt."
    child.persona = (base + addendum).strip() if base else addendum.strip()


def _persona_inherit(parent: "Agent", child: "Agent") -> None:
    """Carry the parent's persona forward unchanged."""
    child.persona = parent.persona


def _swap_model(alternatives: list[str]) -> Callable[["Agent", "Agent"], None]:
    """Build a mutation that swaps the model to a fixed alternative."""
    def _apply(parent: "Agent", child: "Agent") -> None:
        # Pick the first alternative that isn't the parent's current model;
        # fall back to the parent's model if every alternative matches.
        for candidate in alternatives:
            if candidate != parent.model:
                child.model = candidate
                return
        child.model = parent.model
    return _apply


def _combined(*moves: Callable[["Agent", "Agent"], None]) -> Callable[["Agent", "Agent"], None]:
    def _apply(parent: "Agent", child: "Agent") -> None:
        for m in moves:
            m(parent, child)
    return _apply


DEFAULT_MUTATIONS: list[Mutation] = [
    Mutation(name="inherit", apply=_persona_inherit),
    Mutation(name="persona-sharpen", apply=_persona_tweak),
]


def mutations_for_nation(nation: "Nation") -> list[Mutation]:
    """Build the mutation set actually available to this nation.

    Model-swap mutations are only useful when there's more than one
    model represented in the citizen pool; otherwise the swap target
    list is empty and the mutation is a no-op. Inferring the swap
    pool from the existing citizens keeps the mutation set honest:
    we only propose changes the user has actually configured for.
    """
    moves = list(DEFAULT_MUTATIONS)
    models = sorted({a.model for a in nation.agents if not a.is_retired})
    if len(models) > 1:
        moves.append(
            Mutation(name="model-swap", apply=_swap_model(models))
        )
        moves.append(
            Mutation(
                name="model-swap+persona",
                apply=_combined(_persona_tweak, _swap_model(models)),
            )
        )
    return moves


# --- reproduction --------------------------------------------------------


# v0.7.3 mutation chooser tunables.
# - epsilon: with this probability we ignore history and pick uniformly,
#   so a slightly-better mutation never permanently starves the others.
# - cold_start_threshold: until we have at least this many observations
#   per mutation, history is too noisy; default to uniform.
MUTATION_EPSILON = 0.2
MUTATION_COLD_START_THRESHOLD = 3


def evaluate_mutation_outcomes(
    nation: "Nation",
    criteria: ReproductionCriteria | None = None,
) -> dict[str, dict[str, float]]:
    """For each mutation type observed on existing children, summarize fitness.

    Returns a dict like:
      {"persona-sharpen": {"count": 4, "avg_fitness": 2.3, "alive_rate": 1.0},
       "model-swap":      {"count": 2, "avg_fitness": 0.5, "alive_rate": 0.5}}

    Used by `choose_mutation_weighted` to bias future picks. "Alive" here
    means the child is not retired or quarantined — a kill signal short
    of measuring quality.
    """
    crit = criteria or ReproductionCriteria()
    by_mutation: dict[str, list[FitnessScore]] = {}
    alive_by_mutation: dict[str, list[bool]] = {}
    for agent in nation.agents:
        mut = getattr(agent, "mutation_from_parent", None)
        if not mut:
            continue
        by_mutation.setdefault(mut, []).append(score_citizen(agent, nation, crit))
        alive_by_mutation.setdefault(mut, []).append(
            not (agent.is_retired or agent.is_quarantined)
        )

    out: dict[str, dict[str, float]] = {}
    for mut, scores in by_mutation.items():
        count = len(scores)
        avg_fit = sum(s.score for s in scores) / count if count else 0.0
        alives = alive_by_mutation.get(mut, [])
        alive_rate = sum(1 for a in alives if a) / len(alives) if alives else 0.0
        out[mut] = {
            "count": float(count),
            "avg_fitness": avg_fit,
            "alive_rate": alive_rate,
        }
    return out


def choose_mutation_weighted(
    moves: list[Mutation],
    nation: "Nation",
    *,
    rng: random.Random,
    epsilon: float = MUTATION_EPSILON,
) -> Mutation:
    """ε-greedy weighted pick from the available mutations.

    With probability `epsilon` we explore (uniform random). Otherwise
    we exploit: weight each mutation by `avg_fitness * alive_rate` of
    historical children. Mutations we've never tried still get the
    average weight so they aren't starved at cold-start. Mutations
    with too-few observations (under cold_start_threshold) also fall
    back to the average — small samples are noisy.

    Why ε-greedy and not Thompson / UCB: those need either Bayesian
    priors per mutation or per-mutation variance, both of which are
    overkill at the population sizes we expect (5–20 citizens per
    nation). ε-greedy is one parameter, predictable, easy to inspect.
    """
    if not moves:
        raise ValueError("choose_mutation_weighted called with empty moves")
    if rng.random() < epsilon:
        return rng.choice(moves)

    outcomes = evaluate_mutation_outcomes(nation)
    weights: list[float] = []
    sample_sizes: list[int] = []
    for m in moves:
        stat = outcomes.get(m.name)
        if not stat or stat["count"] < MUTATION_COLD_START_THRESHOLD:
            # Not enough data — placeholder, replaced below with avg weight
            weights.append(-1.0)
            sample_sizes.append(int(stat["count"]) if stat else 0)
            continue
        # Combined score: how strong AND how alive. Both bounded so the
        # combined weight stays modest, preserving exploration potential.
        score = max(0.01, stat["avg_fitness"] * max(0.1, stat["alive_rate"]))
        weights.append(score)
        sample_sizes.append(int(stat["count"]))

    # Fill cold-start placeholders with the AVERAGE of observed weights
    # (so an untried mutation isn't penalized for being untried).
    observed = [w for w in weights if w >= 0]
    fill = sum(observed) / len(observed) if observed else 1.0
    weights = [fill if w < 0 else w for w in weights]

    return rng.choices(moves, weights=weights, k=1)[0]


def reproduce(
    nation: "Nation",
    parent: "Agent",
    *,
    mutation: Mutation | None = None,
    rng: random.Random | None = None,
    use_history: bool = True,
) -> Lineage:
    """Spawn a child of `parent` and add it to the nation.

    When `mutation` is None and `use_history` is True (default), we use
    `choose_mutation_weighted` — ε-greedy biased by past offspring's
    fitness. Pass `mutation=` for deterministic behavior (CLI flag, tests),
    or `use_history=False` to fall back to the v0.3.1 uniform-random pick.
    """
    from anthill.core.agent import Agent  # local import — avoids cycles
    rng = rng or random.Random()
    moves = mutations_for_nation(nation)
    if mutation is not None:
        chosen = mutation
    elif use_history:
        chosen = choose_mutation_weighted(moves, nation, rng=rng)
    else:
        chosen = rng.choice(moves)

    child = Agent(
        model=parent.model,  # default; mutation may overwrite
        persona=parent.persona,
        parent_id=parent.id,
        generation=parent.generation + 1,
        mutation_from_parent=chosen.name,
    )
    chosen.apply(parent, child)
    nation.agents.append(child)

    notes: list[str] = []
    if child.model != parent.model:
        notes.append(f"model: {parent.model} → {child.model}")
    if child.persona != parent.persona:
        # Keep notes brief; the full persona is on the Agent itself.
        old_len = len(parent.persona or "")
        new_len = len(child.persona or "")
        notes.append(f"persona: {old_len} → {new_len} chars")
    if not notes:
        notes.append("inherited unchanged")

    return Lineage(child=child, parent=parent, mutation=chosen, notes=notes)


def auto_reproduce(
    nation: "Nation",
    *,
    criteria: ReproductionCriteria | None = None,
    rng: random.Random | None = None,
    max_births: int | None = None,
) -> list[Lineage]:
    """Reproduce every qualifying citizen once. Returns one Lineage per birth.

    `max_births` caps the total so a freshly-tightened criteria can't
    accidentally double the nation in a single pass. When None, every
    qualifier reproduces.
    """
    crit = criteria or ReproductionCriteria()
    ranked = rank_citizens(nation, crit)
    qualifiers = [s for s in ranked if s.qualifies]
    if max_births is not None:
        qualifiers = qualifiers[:max_births]

    lineages: list[Lineage] = []
    for s in qualifiers:
        parent = next((a for a in nation.agents if a.id == s.agent_id), None)
        if parent is None:
            continue
        lineages.append(reproduce(nation, parent, rng=rng))
    return lineages


# --- lineage walking -----------------------------------------------------


def ancestors_of(nation: "Nation", agent_id: str) -> list["Agent"]:
    """Walk parent_id back to the root founder. Excludes the agent itself."""
    by_id = {a.id: a for a in nation.agents}
    chain: list["Agent"] = []
    current = by_id.get(agent_id)
    if current is None:
        return chain
    visited: set[str] = {current.id}
    while current.parent_id is not None:
        parent = by_id.get(current.parent_id)
        if parent is None or parent.id in visited:
            break  # broken or cyclic lineage — stop walking
        visited.add(parent.id)
        chain.append(parent)
        current = parent
    return chain


def descendants_of(nation: "Nation", agent_id: str) -> list["Agent"]:
    """Every citizen whose ancestry traces back to `agent_id`."""
    children_by_parent: dict[str, list["Agent"]] = {}
    for a in nation.agents:
        if a.parent_id:
            children_by_parent.setdefault(a.parent_id, []).append(a)

    result: list["Agent"] = []
    stack: list[str] = [agent_id]
    while stack:
        pid = stack.pop()
        for child in children_by_parent.get(pid, []):
            result.append(child)
            stack.append(child.id)
    return result


__all__ = [
    "FitnessScore",
    "ReproductionCriteria",
    "Mutation",
    "Lineage",
    "DEFAULT_MUTATIONS",
    "DEFAULT_MIN_FITNESS",
    "DEFAULT_MIN_TASK_TYPES",
    "MUTATION_EPSILON",
    "MUTATION_COLD_START_THRESHOLD",
    "score_citizen",
    "rank_citizens",
    "mutations_for_nation",
    "reproduce",
    "auto_reproduce",
    "ancestors_of",
    "descendants_of",
    "evaluate_mutation_outcomes",
    "choose_mutation_weighted",
]
