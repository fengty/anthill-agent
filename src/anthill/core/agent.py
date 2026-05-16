"""An Agent is a worker ant — generic at birth, specialized through experience."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from anthill.models import ModelProvider, get_provider


@dataclass
class TaskResult:
    """The outcome of an agent attempting a task.

    `success_score` stays a single [0, 1] scalar — it answers the binary
    "did this attempt produce something usable" question and drives the
    pheromone deposit/alarm decision. `scores` is the open-vocabulary
    multi-dim view: whichever dimensions the judge or the user actually
    talked about (correctness, conciseness, citation_quality, whatever).
    `scores` can be empty when no judge ran; it isn't a failure signal,
    just absence of multi-dim data.
    """

    task_id: str
    agent_id: str
    task_type: str
    output: Any
    success_score: float  # [0, 1] — pheromone deposit multiplier
    duration_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    # Open-vocabulary dimension scores from v0.4 onward. Whatever the
    # judge (or the user, via `anthill rate --dim`) named, lives here.
    # Keys are normalized via core.values.normalize_dim.
    scores: dict[str, float] = field(default_factory=dict)
    # Structured failure attribution (v0.5+). Stored as the enum's string
    # value so JSON round-trip is trivial. None when the attempt
    # succeeded.
    failure_reason: str | None = None


@dataclass
class Agent:
    """A citizen of a nation.

    Citizens start identical in the simple case. Specialization comes from
    the pheromone trails they accumulate, not from a `role` field assigned
    by a human.

    For benchmarks and experiments, a citizen can carry a `persona` —
    a system prompt baked in at spawn time. This is what creates the
    latent capability differences that the pheromone mechanism is
    supposed to discover.

    Lifecycle: `retired_at` is the soft-delete marker. A retired citizen
    stays in the nation (so pheromone trails and history still resolve
    its id) but the router will not assign new tasks to it. Retirement
    is reversible — `Nation.unretire(id)` clears the field. Citizens
    can be retired manually (`anthill citizen retire`) or in bulk by
    the lifecycle module's stale-citizen sweep.
    """

    id: str = field(default_factory=lambda: f"ant-{uuid.uuid4().hex[:8]}")
    model: str = "deepseek-chat"
    persona: str | None = None
    private_memory: dict[str, Any] = field(default_factory=dict)
    born_at: float = field(default_factory=time.time)
    retired_at: float | None = None
    # Lineage: parent_id is the citizen that spawned this one via
    # reproduction (v0.3.1). generation is 0 for citizens spawned by the
    # user via `anthill spawn`, +1 for each descendant step. The pair
    # lets a future `anthill citizen family` walk an ancestor tree.
    parent_id: str | None = None
    generation: int = 0
    # Quarantine: v0.5+. Set by the immune system when the citizen's
    # recent failures cross a threshold. Distinct from retirement:
    # retirement is "user / lifecycle says this citizen is done";
    # quarantine is "temporary observation — may auto-release."
    quarantined_at: float | None = None
    quarantine_reason: str | None = None
    _provider: ModelProvider | None = field(default=None, repr=False)

    @property
    def is_retired(self) -> bool:
        return self.retired_at is not None

    @property
    def is_quarantined(self) -> bool:
        return self.quarantined_at is not None

    @property
    def is_available(self) -> bool:
        """True when the router may assign new work."""
        return not self.is_retired and not self.is_quarantined

    def _get_provider(self) -> ModelProvider:
        if self._provider is None:
            self._provider = get_provider(self.model)
        return self._provider

    async def execute(
        self,
        task_type: str,
        prompt: str,
        *,
        system: str | None = None,
    ) -> TaskResult:
        """Run one task. The nation scores the result and deposits pheromone.

        Success scoring is intentionally crude in v0.0.2: a non-empty,
        non-error response scores 1.0; an exception scores 0.0. Real
        scoring (LLM-judge, task-specific rubrics) lives in v0.0.4.
        """
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        provider = self._get_provider()
        effective_system = system if system is not None else self.persona
        start = time.perf_counter()

        try:
            response = await provider.complete(prompt, system=effective_system)
            duration = time.perf_counter() - start
            success_score = 1.0 if response.text.strip() else 0.0
            from anthill.core.failure import classify_attempt
            reason = classify_attempt(
                response.text, exception=None, success_score=success_score
            )
            return TaskResult(
                task_id=task_id,
                agent_id=self.id,
                task_type=task_type,
                output=response.text,
                success_score=success_score,
                duration_seconds=duration,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                failure_reason=reason.value if reason is not None else None,
            )
        except Exception as e:  # noqa: BLE001 — we want any failure to erode the trail
            duration = time.perf_counter() - start
            from anthill.core.failure import classify_attempt
            reason = classify_attempt(
                f"[error] {e}", exception=e, success_score=0.0
            )
            return TaskResult(
                task_id=task_id,
                agent_id=self.id,
                task_type=task_type,
                output=f"[error] {e}",
                success_score=0.0,
                duration_seconds=duration,
                failure_reason=reason.value if reason is not None else None,
            )
