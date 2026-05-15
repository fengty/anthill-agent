"""Colony — the top-level container that ties agents, pheromones, and router together."""

from __future__ import annotations

from dataclasses import dataclass, field

from anthill.core.agent import Agent, TaskResult
from anthill.core.culture import Culture
from anthill.core.pheromone import PheromoneTrail
from anthill.core.router import Router, RouterConfig
from anthill.core.scout import Plan, Scout


@dataclass
class AskResult:
    """The aggregated outcome of a natural-language request.

    A single user request may produce one or many subtask results. We keep
    both so callers can either show a final answer or inspect what happened.
    """

    request: str
    plan: Plan
    results: list[TaskResult]

    @property
    def final_output(self) -> str:
        """Concatenate subtask outputs in plan order."""
        return "\n\n".join(str(r.output) for r in self.results)


@dataclass
class Colony:
    """A working ant colony.

    Holds the agents, the pheromone map, and the router. This is what users
    interact with — `colony.run(task)` does the whole pheromone loop.
    """

    name: str = "default"
    agents: list[Agent] = field(default_factory=list)
    pheromones: PheromoneTrail = field(default_factory=PheromoneTrail)
    culture: Culture = field(default_factory=Culture)
    router_config: RouterConfig = field(default_factory=RouterConfig)
    scout_model: str = "deepseek-chat"

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

    def _compose_system(self, agent: Agent) -> str | None:
        """Combine agent persona + colony house_style into a single system prompt.

        Persona is the agent's individual disposition. House style is the
        colony's shared voice. Both apply at once: the agent answers in its
        own way, within the colony's conventions.
        """
        parts: list[str] = []
        if agent.persona:
            parts.append(agent.persona.strip())
        style = self.culture.house_style.strip() if self.culture.house_style else ""
        if style:
            parts.append("Colony house style:\n" + style)
        return "\n\n".join(parts) or None

    async def run(self, task_type: str, prompt: str) -> TaskResult:
        """Execute one typed task: route, run, deposit pheromone."""
        agent = self.router.assign(task_type)
        result = await agent.execute(task_type, prompt, system=self._compose_system(agent))
        self.pheromones.deposit(
            agent_id=result.agent_id,
            task_type=result.task_type,
            success_score=result.success_score,
        )
        # The catalog records every attempted task, not just successful ones,
        # because the colony's vocabulary is what work it tries, not just what
        # it succeeds at.
        self.culture.record(task_type)
        return result

    async def ask(self, request: str) -> AskResult:
        """Execute a natural-language request.

        The Scout decomposes the request into typed subtasks; each subtask
        runs through the normal pheromone-routed pipeline. Dependencies are
        respected by executing subtasks sequentially in plan order (a real
        DAG executor can replace this when subtasks need to run in parallel).

        The colony's existing task-type vocabulary is fed to Scout so it
        prefers reusing established labels — keeping pheromone trails
        concentrated instead of fragmenting them into one-shot categories.
        """
        scout = Scout(model=self.scout_model)
        plan = await scout.plan(request, known_task_types=self.culture.known_task_types())

        results: list[TaskResult] = []
        for subtask in plan.subtasks:
            result = await self.run(subtask.task_type, subtask.prompt)
            results.append(result)
        return AskResult(request=request, plan=plan, results=results)
