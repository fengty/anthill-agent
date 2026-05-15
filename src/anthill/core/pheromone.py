"""Pheromone trails — the mechanism by which specialization emerges.

Each task a citizen completes deposits a pheromone on the (agent, task_type)
edge. Trails strengthen with successful completions and decay over time.
The router reads trails to bias future task assignment.

Real ants also have *alarm* pheromones — chemicals that mark a path or
a target as dangerous, repelling rather than attracting. We mirror that
here as a second number per trail: `alarm`. A failure does not just fail
to deposit success pheromone; it deposits alarm. The router subtracts
alarm from success when picking a citizen, so a known-bad path can
*actively repel* — not merely fail to attract.

This is the entire core insight of Anthill, in one file.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class Trail:
    """A single pheromone trail.

    `strength` accumulates success (attractor).
    `alarm`    accumulates failure (repellor) — separate from strength so
               a chronic failure agent does not just lose ground, it
               actively repels new attempts.
    """

    agent_id: str
    task_type: str
    strength: float = 0.0
    alarm: float = 0.0
    last_updated: float = field(default_factory=time.time)

    @property
    def net(self) -> float:
        """Routing score: attraction minus repulsion. Never negative."""
        return max(0.0, self.strength - self.alarm)


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
        alarm_amount: float = 0.5,
    ) -> None:
        # decay_rate: fraction of strength lost per hour of inactivity.
        # Keep it low — nations should remember, but not forever.
        # alarm_amount: how much repulsion a single failure deposits.
        # Smaller than success deposit so one bad day does not blacklist
        # an otherwise good citizen.
        self.decay_rate = decay_rate
        self.deposit_amount = deposit_amount
        self.max_strength = max_strength
        self.alarm_amount = alarm_amount
        self._trails: dict[tuple[str, str], Trail] = {}

    def deposit(self, agent_id: str, task_type: str, success_score: float = 1.0) -> None:
        """Reinforce a trail after a task attempt.

        success_score > 0  → strength increases (attractor pheromone)
        success_score == 0 → alarm increases (alarm pheromone)
        success_score < 0  → strength erodes by |score| (manual erosion,
                             used by `anthill rate down`)

        The result is a trail that can both attract and repel,
        modelling the two-channel chemistry of real ant colonies.
        """
        key = (agent_id, task_type)
        trail = self._trails.get(key) or Trail(agent_id=agent_id, task_type=task_type)
        trail.strength = self._apply_decay(trail.strength, trail.last_updated)
        trail.alarm = self._apply_decay(trail.alarm, trail.last_updated)

        if success_score > 0:
            trail.strength = min(
                trail.strength + self.deposit_amount * success_score,
                self.max_strength,
            )
        elif success_score == 0:
            # Failure: deposit alarm rather than erode strength. A failure
            # is information about danger, separate from absence of success.
            trail.alarm = min(trail.alarm + self.alarm_amount, self.max_strength)
        else:
            # Negative score: explicit erosion (e.g. king's thumbs-down).
            trail.strength = max(0.0, trail.strength + self.deposit_amount * success_score)

        trail.last_updated = time.time()
        self._trails[key] = trail

    def strength(self, agent_id: str, task_type: str) -> float:
        """Current routing score after decay (strength minus alarm, never negative)."""
        trail = self._trails.get((agent_id, task_type))
        if trail is None:
            return 0.0
        decayed_strength = self._apply_decay(trail.strength, trail.last_updated)
        decayed_alarm = self._apply_decay(trail.alarm, trail.last_updated)
        return max(0.0, decayed_strength - decayed_alarm)

    def alarm(self, agent_id: str, task_type: str) -> float:
        """Current alarm signal after decay."""
        trail = self._trails.get((agent_id, task_type))
        if trail is None:
            return 0.0
        return self._apply_decay(trail.alarm, trail.last_updated)

    def strongest_for(self, task_type: str) -> Trail | None:
        """Return the citizen whose trail has the highest net score."""
        candidates = []
        for (_, tt), t in self._trails.items():
            if tt != task_type:
                continue
            net = max(
                0.0,
                self._apply_decay(t.strength, t.last_updated)
                - self._apply_decay(t.alarm, t.last_updated),
            )
            candidates.append((net, t))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def ranking(self, task_type: str) -> list[tuple[str, float]]:
        """All citizens ranked by net routing score for this task type."""
        ranked: list[tuple[str, float]] = []
        for (_, tt), t in self._trails.items():
            if tt != task_type:
                continue
            net = max(
                0.0,
                self._apply_decay(t.strength, t.last_updated)
                - self._apply_decay(t.alarm, t.last_updated),
            )
            ranked.append((t.agent_id, net))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    def trails(self) -> Iterator[Trail]:
        """Iterate all trails with decay applied to both channels."""
        for trail in self._trails.values():
            trail.strength = self._apply_decay(trail.strength, trail.last_updated)
            trail.alarm = self._apply_decay(trail.alarm, trail.last_updated)
            yield trail

    def _apply_decay(self, value: float, last_updated: float) -> float:
        """Exponential decay applied to either strength or alarm."""
        hours_elapsed = (time.time() - last_updated) / 3600.0
        if hours_elapsed <= 0:
            return value
        return value * math.exp(-self.decay_rate * hours_elapsed)
