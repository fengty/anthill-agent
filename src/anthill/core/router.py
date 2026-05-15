"""Router — picks an agent for a task based on pheromone trails.

This is the heart of Anthill's difference from role-based frameworks.
There is no `if task_type == "code": return coder_agent`. There is only
the pheromone map and a selection policy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from anthill.core.agent import Agent
from anthill.core.pheromone import PheromoneTrail


@dataclass
class RouterConfig:
    """Routing policy knobs.

    exploration: probability of picking a non-strongest agent. Without
    exploration, the first ant to walk a path takes it forever. Ant colonies
    keep ~5–15% exploration noise so they can find better paths.
    """

    exploration: float = 0.10
    cold_start_random: bool = True


class Router:
    """Routes tasks to agents based on pheromone trail strength."""

    def __init__(
        self,
        pheromones: PheromoneTrail,
        agents: list[Agent],
        config: RouterConfig | None = None,
    ) -> None:
        self.pheromones = pheromones
        self.agents = agents
        self.config = config or RouterConfig()

    def assign(self, task_type: str, *, forbid: set[str] | None = None) -> Agent:
        """Pick a citizen for this task type.

        `forbid` is the set of citizen IDs that must NOT be picked — used
        on retry, to try someone other than whoever just failed. If
        forbidding everyone, raises RuntimeError; the executor catches it
        and marks the subtask as unrecoverable.
        """
        if not self.agents:
            raise RuntimeError("No citizens in the nation.")

        forbid = forbid or set()
        candidates = [a for a in self.agents if a.id not in forbid]
        if not candidates:
            raise RuntimeError(
                f"No citizens available for '{task_type}': every candidate is "
                f"forbidden (likely all of them have already failed this attempt)."
            )

        # Exploration: occasionally pick a random eligible citizen.
        if random.random() < self.config.exploration:
            return random.choice(candidates)

        # A trail with strength 0 means "tried and failed" — should not
        # outrank citizens that haven't tried at all.
        ranking = [
            (aid, s)
            for aid, s in self.pheromones.ranking(task_type)
            if s > 0 and aid not in forbid
        ]

        tried_ids = {aid for aid, _ in self.pheromones.ranking(task_type)}
        untried = [a for a in candidates if a.id not in tried_ids]
        if untried and not ranking:
            return random.choice(untried)

        if not ranking and self.config.cold_start_random:
            return random.choice(candidates)

        if ranking:
            best_id = ranking[0][0]
            for agent in candidates:
                if agent.id == best_id:
                    return agent

        return random.choice(candidates)
