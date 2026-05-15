"""Run both routing strategies on identical task pools and report the gap.

The agents are deliberately given personas that bias them toward one task
type. Neither strategy is told about these personas; they must discover them
(pheromone) or fail to (role).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from anthill.bench.runners import (
    StrategyResult,
    run_pheromone_strategy,
    run_role_strategy,
)
from anthill.bench.tasks import generate_pool
from anthill.core.agent import Agent


TERSE_PERSONA = (
    "You answer in 1-3 words. Never use a full sentence. Never explain. "
    "Just the answer."
)
VERBOSE_PERSONA = (
    "You always explain in detail. Give context, examples, and reasoning. "
    "Use at least 30 words."
)


@dataclass
class BenchmarkResult:
    role: StrategyResult
    pheromone: StrategyResult

    @property
    def gap(self) -> float:
        return self.pheromone.mean - self.role.mean

    def summary(self) -> str:
        return (
            f"role        avg score = {self.role.mean:.2%}  ({self.role.n} tasks)\n"
            f"pheromone   avg score = {self.pheromone.mean:.2%}  ({self.pheromone.n} tasks)\n"
            f"gap         {self.gap:+.2%}"
        )


def build_personas(
    n_terse: int = 2,
    n_verbose: int = 2,
    model: str = "deepseek-chat",
) -> list[Agent]:
    """Create the experimental agent population.

    The benchmark mixes terse and verbose agents. Neither strategy gets to
    see the personas; they must infer capability from outcomes.
    """
    agents: list[Agent] = []
    for _ in range(n_terse):
        agents.append(Agent(model=model, persona=TERSE_PERSONA))
    for _ in range(n_verbose):
        agents.append(Agent(model=model, persona=VERBOSE_PERSONA))
    return agents


async def benchmark(
    *,
    n_terse_tasks: int = 25,
    n_verbose_tasks: int = 25,
    n_terse_agents: int = 2,
    n_verbose_agents: int = 2,
    model: str = "deepseek-chat",
    exploration: float = 0.10,
    seed: int = 42,
) -> BenchmarkResult:
    tasks = generate_pool(n_terse=n_terse_tasks, n_verbose=n_verbose_tasks, seed=seed)

    # Fresh agents per arm so private state doesn't leak between strategies.
    role_agents = build_personas(n_terse_agents, n_verbose_agents, model=model)
    pher_agents = build_personas(n_terse_agents, n_verbose_agents, model=model)

    role_task = run_role_strategy(role_agents, tasks, seed=seed)
    pher_task = run_pheromone_strategy(pher_agents, tasks, exploration=exploration)
    role_result, pher_result = await asyncio.gather(role_task, pher_task)
    return BenchmarkResult(role=role_result, pheromone=pher_result)
