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

    def assign(self, task_type: str) -> Agent:
        """Pick an agent for this task type."""
        if not self.agents:
            raise RuntimeError("No citizens in the nation.")

        # Exploration: occasionally pick a random agent to find better paths.
        if random.random() < self.config.exploration:
            return random.choice(self.agents)

        # A trail with strength 0 means "tried and failed" — should not
        # outrank agents that haven't tried at all. Treat zero-strength
        # entries as untried so they don't block cold-start randomness.
        ranking = [(aid, s) for aid, s in self.pheromones.ranking(task_type) if s > 0]

        tried_ids = {aid for aid, _ in self.pheromones.ranking(task_type)}
        untried = [a for a in self.agents if a.id not in tried_ids]
        if untried and not ranking:
            # Nobody has succeeded yet — explore among the agents that haven't
            # been tried, instead of doubling down on a known failure.
            return random.choice(untried)

        if not ranking and self.config.cold_start_random:
            return random.choice(self.agents)

        if ranking:
            best_id = ranking[0][0]
            for agent in self.agents:
                if agent.id == best_id:
                    return agent

        return random.choice(self.agents)
