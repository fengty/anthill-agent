"""Nation — the top-level entity that organises agents, pheromones, and culture.

A Nation is what the user actually owns. The framework supplies the
mechanics — pheromone trails, scouts, routing — and the Nation is the
living thing that grows on top of them. One user, one Nation, many
agents serving the user the way citizens serve a king.

There is no upper bound on size. A Nation can start with three workers
and grow to thousands. The point of the design is that the same
mechanism scales.
"""

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
class Nation:
    """A working ant nation — the user's AI organisation.

    Holds the agents (citizens), the pheromone map (the nation's accumulated
    expertise), the culture (its identity and conventions), and the router.
    This is what users interact with — `nation.run(task)` does the whole
    pheromone loop end-to-end.
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
        """Add new citizens to the nation."""
        new_agents = [Agent(model=model, persona=persona) for _ in range(count)]
        self.agents.extend(new_agents)
        return new_agents

    @property
    def router(self) -> Router:
        return Router(self.pheromones, self.agents, self.router_config)

    def _compose_system(self, agent: Agent) -> str | None:
        """Combine agent persona + nation house_style into a single system prompt.

        Persona is the agent's individual disposition. House style is the
        nation's shared voice. Both apply at once: the agent answers in its
        own way, within the nation's conventions.
        """
        parts: list[str] = []
        if agent.persona:
            parts.append(agent.persona.strip())
        style = self.culture.house_style.strip() if self.culture.house_style else ""
        if style:
            parts.append("Nation house style:\n" + style)
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
        # The catalog records every attempted task, not just successful ones —
        # the nation's vocabulary is the work it tries, not only what it
        # succeeds at.
        self.culture.record(task_type)
        return result

    async def ask(self, request: str) -> AskResult:
        """Execute a natural-language request from the king.

        The Scout decomposes the request into typed subtasks; each subtask
        runs through the normal pheromone-routed pipeline. Dependencies are
        respected by executing subtasks sequentially in plan order (a real
        DAG executor can replace this when subtasks need to run in parallel).

        The nation's existing task-type vocabulary is fed to Scout so it
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
