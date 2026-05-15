"""Pheromone trails — the mechanism by which specialization emerges.

Each task an agent completes deposits a pheromone on the (agent, task_type) edge.
Trails strengthen with successful completions and decay over time.
The router reads trails to bias future task assignment.

This is the entire core insight of Anthill, in one file.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class Trail:
    """A single pheromone trail: one agent's reinforced ability at one task type."""

    agent_id: str
    task_type: str
    strength: float = 0.0
    last_updated: float = field(default_factory=time.time)


class PheromoneTrail:
    """The colony's pheromone map.

    Strength rises with success, decays with time. No central planner —
    routing decisions just read this map.
    """

    def __init__(
        self,
        decay_rate: float = 0.05,
        deposit_amount: float = 1.0,
        max_strength: float = 100.0,
    ) -> None:
        # decay_rate: fraction of strength lost per hour of inactivity.
        # Keep it low — colonies should remember, but not forever.
        self.decay_rate = decay_rate
        self.deposit_amount = deposit_amount
        self.max_strength = max_strength
        self._trails: dict[tuple[str, str], Trail] = {}

    def deposit(self, agent_id: str, task_type: str, success_score: float = 1.0) -> None:
        """Reinforce a trail after a successful (or failed) task.

        success_score in [0, 1]. 1.0 = full reinforcement, 0.0 = no deposit,
        negative scores erode the trail (failure leaves negative signal).
        """
        key = (agent_id, task_type)
        trail = self._trails.get(key) or Trail(agent_id=agent_id, task_type=task_type)
        trail.strength = self._apply_decay(trail)
        trail.strength = min(trail.strength + self.deposit_amount * success_score, self.max_strength)
        trail.strength = max(trail.strength, 0.0)
        trail.last_updated = time.time()
        self._trails[key] = trail

    def strength(self, agent_id: str, task_type: str) -> float:
        """Current pheromone strength after applying decay."""
        trail = self._trails.get((agent_id, task_type))
        if trail is None:
            return 0.0
        return self._apply_decay(trail)

    def strongest_for(self, task_type: str) -> Trail | None:
        """Return the agent with the strongest trail for this task type."""
        candidates = [
            (self._apply_decay(t), t) for (_, tt), t in self._trails.items() if tt == task_type
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def ranking(self, task_type: str) -> list[tuple[str, float]]:
        """All agents ranked by trail strength for this task type."""
        ranked = [
            (t.agent_id, self._apply_decay(t))
            for (_, tt), t in self._trails.items()
            if tt == task_type
        ]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    def trails(self) -> Iterator[Trail]:
        """Iterate all trails (with decay applied)."""
        for trail in self._trails.values():
            trail.strength = self._apply_decay(trail)
            yield trail

    def _apply_decay(self, trail: Trail) -> float:
        """Exponential decay based on time since last deposit."""
        hours_elapsed = (time.time() - trail.last_updated) / 3600.0
        if hours_elapsed <= 0:
            return trail.strength
        return trail.strength * math.exp(-self.decay_rate * hours_elapsed)
