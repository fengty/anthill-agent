"""Tests for routing decisions, especially the zero-strength fallback.

The original bug: a failed task left a strength-0 trail. Because the trail
existed, the router's `ranking` was non-empty, so the cold-start random
path never triggered again — and the router kept picking the failed agent.

The fix: treat strength-0 trails as 'tried and failed'. Prefer untried
agents over known-failed agents.
"""

from __future__ import annotations

import random

from anthill.core.agent import Agent
from anthill.core.pheromone import PheromoneTrail
from anthill.core.router import Router, RouterConfig


def test_zero_strength_trail_does_not_lock_in_failed_agent() -> None:
    """An agent that failed should not keep getting picked over untried agents."""
    p = PheromoneTrail()
    a, b, c = Agent(id="a"), Agent(id="b"), Agent(id="c")
    p.deposit("a", "task", success_score=0.0)  # 'a' tried and failed

    # No exploration, no randomness — the router should still avoid 'a'.
    router = Router(p, [a, b, c], RouterConfig(exploration=0.0))
    random.seed(0)
    picks = [router.assign("task").id for _ in range(20)]
    assert "a" not in picks  # the known-failure agent never gets picked
    assert set(picks) <= {"b", "c"}


def test_successful_trail_wins_over_untried() -> None:
    """Once an agent has strength > 0, it should be preferred over untried."""
    p = PheromoneTrail()
    a, b, c = Agent(id="a"), Agent(id="b"), Agent(id="c")
    p.deposit("b", "task", success_score=1.0)

    router = Router(p, [a, b, c], RouterConfig(exploration=0.0))
    for _ in range(10):
        assert router.assign("task").id == "b"


def test_strongest_wins_when_multiple_have_succeeded() -> None:
    p = PheromoneTrail()
    a, b, c = Agent(id="a"), Agent(id="b"), Agent(id="c")
    p.deposit("a", "task", success_score=1.0)
    p.deposit("b", "task", success_score=1.0)
    p.deposit("b", "task", success_score=1.0)  # 'b' has the strongest trail

    router = Router(p, [a, b, c], RouterConfig(exploration=0.0))
    for _ in range(10):
        assert router.assign("task").id == "b"


def test_exploration_does_pick_others() -> None:
    """With exploration=1.0, the router should pick uniformly at random."""
    p = PheromoneTrail()
    agents = [Agent(id=f"a{i}") for i in range(5)]
    p.deposit("a0", "task", success_score=1.0)

    router = Router(p, agents, RouterConfig(exploration=1.0))
    random.seed(0)
    picks = {router.assign("task").id for _ in range(100)}
    # All five agents should appear over 100 random picks.
    assert len(picks) == 5


def test_empty_pheromones_uses_cold_start_random() -> None:
    p = PheromoneTrail()
    agents = [Agent(id=f"a{i}") for i in range(3)]
    router = Router(p, agents, RouterConfig(exploration=0.0))

    random.seed(0)
    picks = {router.assign("task").id for _ in range(50)}
    assert len(picks) == 3  # all three get sampled
