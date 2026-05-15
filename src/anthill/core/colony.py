"""Colony — the top-level container that ties agents, pheromones, and router together."""

from __future__ import annotations

from dataclasses import dataclass, field

from anthill.core.agent import Agent, TaskResult
from anthill.core.pheromone import PheromoneTrail
from anthill.core.router import Router, RouterConfig


@dataclass
class Colony:
    """A working ant colony.

    Holds the agents, the pheromone map, and the router. This is what users
    interact with — `colony.run(task)` does the whole pheromone loop.
    """

    name: str = "default"
    agents: list[Agent] = field(default_factory=list)
    pheromones: PheromoneTrail = field(default_factory=PheromoneTrail)
    router_config: RouterConfig = field(default_factory=RouterConfig)

    def spawn(
        self,
        count: int = 1,
        model: str = "deepseek-chat",
        persona: str | None = None,
    ) -> list[Agent]:
        """Add new generic workers to the colony."""
        new_agents = [Agent(model=model, persona=persona) for _ in range(count)]
        self.agents.extend(new_agents)
        return new_agents

    @property
    def router(self) -> Router:
        return Router(self.pheromones, self.agents, self.router_config)

    async def run(self, task_type: str, prompt: str) -> TaskResult:
        """Execute one task: route, run, deposit pheromone."""
        agent = self.router.assign(task_type)
        result = await agent.execute(task_type, prompt)
        self.pheromones.deposit(
            agent_id=result.agent_id,
            task_type=result.task_type,
            success_score=result.success_score,
        )
        return result
