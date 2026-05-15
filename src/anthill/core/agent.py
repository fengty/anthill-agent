"""An Agent is a worker ant — generic at birth, specialized through experience."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskResult:
    """The outcome of an agent attempting a task."""

    task_id: str
    agent_id: str
    task_type: str
    output: Any
    success_score: float  # [0, 1] — pheromone deposit multiplier
    duration_seconds: float


@dataclass
class Agent:
    """A worker in the colony.

    Agents start identical. Specialization comes from the pheromone trails
    they accumulate over time, not from a `role` field assigned by a human.
    """

    id: str = field(default_factory=lambda: f"ant-{uuid.uuid4().hex[:8]}")
    model: str = "claude-sonnet-4-5"
    private_memory: dict[str, Any] = field(default_factory=dict)

    async def execute(self, task_type: str, prompt: str) -> TaskResult:
        """Execute a task. Returns a TaskResult; the colony deposits pheromones from it.

        Implementation pending — this is the model dispatch layer.
        """
        raise NotImplementedError("Model dispatch coming in v0.0.2")
