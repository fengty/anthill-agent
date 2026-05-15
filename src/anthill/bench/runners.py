"""Two routing strategies, one task pool, one fair comparison.

RoleRunner — assigns each task type to a fixed agent (the "planned economy"
baseline). The pre-assignment is blind: the human doesn't know which agent
is actually good at which task type, so the assignment is random.

PheromoneRunner — uses the colony's pheromone-based router. Specialization
emerges from cold-start randomness plus trail reinforcement.

Both runners see the SAME agents and the SAME tasks. The only thing that
differs is the routing policy.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from anthill.bench.evaluator import score
from anthill.bench.tasks import Task
from anthill.core.agent import Agent
from anthill.core.pheromone import PheromoneTrail
from anthill.core.router import Router, RouterConfig


@dataclass
class RunRecord:
    """One task's outcome under a routing strategy."""

    task_idx: int
    task_type: str
    agent_id: str
    persona: str | None
    score: float
    response: str


@dataclass
class StrategyResult:
    """Aggregated results for one strategy across a task pool."""

    strategy: str
    records: list[RunRecord] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        return sum(r.score for r in self.records)

    @property
    def n(self) -> int:
        return len(self.records)

    @property
    def mean(self) -> float:
        return self.total_score / self.n if self.n else 0.0

    def rolling(self, window: int = 10) -> list[float]:
        """Rolling mean score across the task sequence — to watch convergence."""
        out = []
        for i in range(len(self.records)):
            start = max(0, i - window + 1)
            chunk = self.records[start : i + 1]
            out.append(sum(r.score for r in chunk) / len(chunk))
        return out


async def run_role_strategy(
    agents: list[Agent],
    tasks: list[Task],
    *,
    seed: int = 7,
) -> StrategyResult:
    """Each task type is bound to one agent at random, then never changes.

    This is what every existing multi-agent framework does today: a human
    decides who is the "translator," who is the "explainer," etc. Without
    knowing the agents' true capabilities, the assignment is essentially
    a coin flip — exactly what we model here.
    """
    rng = random.Random(seed)
    task_types = sorted({t.task_type for t in tasks})
    assignment: dict[str, Agent] = {tt: rng.choice(agents) for tt in task_types}

    result = StrategyResult(strategy="role")
    for i, task in enumerate(tasks):
        agent = assignment[task.task_type]
        task_result = await agent.execute(task.task_type, task.prompt)
        s = score(task, str(task_result.output))
        result.records.append(
            RunRecord(
                task_idx=i,
                task_type=task.task_type,
                agent_id=agent.id,
                persona=agent.persona,
                score=s,
                response=str(task_result.output),
            )
        )
    return result


async def run_pheromone_strategy(
    agents: list[Agent],
    tasks: list[Task],
    *,
    exploration: float = 0.10,
) -> StrategyResult:
    """Pheromone-based routing: cold start random, trails reinforce winners."""
    pheromones = PheromoneTrail()
    router = Router(pheromones, agents, RouterConfig(exploration=exploration))

    result = StrategyResult(strategy="pheromone")
    for i, task in enumerate(tasks):
        agent = router.assign(task.task_type)
        task_result = await agent.execute(task.task_type, task.prompt)
        s = score(task, str(task_result.output))
        pheromones.deposit(agent.id, task.task_type, success_score=s)
        result.records.append(
            RunRecord(
                task_idx=i,
                task_type=task.task_type,
                agent_id=agent.id,
                persona=agent.persona,
                score=s,
                response=str(task_result.output),
            )
        )
    return result
