"""An Agent is a worker ant — generic at birth, specialized through experience."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from anthill.models import ModelProvider, get_provider


# Callback signature for live streaming: invoked with each incremental
# text delta as the provider produces it. Used by the REPL to render
# tokens as they arrive instead of waiting for the full response.
TokenCallback = Callable[[str], Awaitable[None]]


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
    # 0.1.26 — True when the provider stopped on max_tokens. Used by
    # the judge to reject mid-sentence answers and by the
    # deliberation loop to know it should run another round with a
    # bigger budget (rather than declaring "100% quality" on a
    # truncated list of 6 items).
    truncated: bool = False


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
    # v0.7.3+: which mutation produced this child from its parent.
    # Empty / None for founder citizens. Used by
    # reproduction.choose_mutation_weighted to make future reproductions
    # bias toward mutations whose offspring have done well historically.
    mutation_from_parent: str | None = None
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
        on_token: TokenCallback | None = None,
        use_agent_loop: bool = False,
        agent_loop_executor=None,
        on_tool_call=None,
        on_tool_result=None,
    ) -> TaskResult:
        """Run one task. The nation scores the result and deposits pheromone.

        Success scoring is intentionally crude in v0.0.2: a non-empty,
        non-error response scores 1.0; an exception scores 0.0. Real
        scoring (LLM-judge, task-specific rubrics) lives in v0.0.4.

        When ``on_token`` is provided (v0.1.10+), this calls the
        provider's streaming interface and invokes the callback with
        each incremental text delta. The final ``TaskResult.output``
        is the concatenation of all deltas — semantically identical
        to the non-streaming path. Callbacks that raise propagate;
        the agent does not swallow REPL render errors silently.
        """
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        provider = self._get_provider()
        effective_system = system if system is not None else self.persona
        start = time.perf_counter()

        # 0.2.30 — agent loop path. When enabled, the model runs in
        # multi-turn ReAct mode with native tool_use. Tool calls
        # (bash_run / browser_action) execute via `agent_loop_executor`
        # and their outputs flow back into the next turn. The loop
        # ends when the model returns no more tool_calls.
        if use_agent_loop:
            try:
                from anthill.core.agent_loop import run_agent_loop
                from anthill.core.tool_executors import dispatch_tool_call
                from anthill.core.tools_protocol import builtin_tools
                from anthill.core.failure import (
                    FailureReason, classify_attempt,
                )

                executor = agent_loop_executor or dispatch_tool_call
                loop_result = await run_agent_loop(
                    provider,
                    system=effective_system,
                    initial_user_message=prompt,
                    # 0.2.30 — include browser tool natively now that
                    # the executor is wired.
                    tools=builtin_tools(include_browser=True),
                    executor=executor,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
                duration = time.perf_counter() - start
                text = loop_result.final_text
                success = 1.0 if text.strip() else 0.0
                if loop_result.stopped_for == "max_iters":
                    success = min(success, 0.5)
                reason = classify_attempt(
                    text, exception=None, success_score=success,
                )
                if loop_result.stopped_for == "max_iters":
                    reason = FailureReason.TRUNCATED
                return TaskResult(
                    task_id=task_id,
                    agent_id=self.id,
                    task_type=task_type,
                    output=text,
                    success_score=success,
                    duration_seconds=duration,
                    input_tokens=loop_result.input_tokens,
                    output_tokens=loop_result.output_tokens,
                    failure_reason=reason.value if reason is not None else None,
                    truncated=loop_result.stopped_for == "max_iters",
                )
            except Exception as e:  # noqa: BLE001 — same handling shape as below
                duration = time.perf_counter() - start
                from anthill.core.failure import classify_attempt
                reason = classify_attempt(
                    f"[error] {e}", exception=e, success_score=0.0,
                )
                return TaskResult(
                    task_id=task_id,
                    agent_id=self.id,
                    task_type=task_type,
                    output=f"[error in agent loop] {e}",
                    success_score=0.0,
                    duration_seconds=duration,
                    failure_reason=reason.value if reason is not None else None,
                )

        try:
            truncated = False
            if on_token is not None:
                parts: list[str] = []
                input_tokens = 0
                output_tokens = 0
                async for chunk in provider.stream(prompt, system=effective_system):
                    if chunk.delta:
                        parts.append(chunk.delta)
                        await on_token(chunk.delta)
                    if chunk.done:
                        input_tokens = chunk.input_tokens
                        output_tokens = chunk.output_tokens
                        if chunk.finish_reason and chunk.finish_reason.lower() in (
                            "length", "max_tokens", "max_output_tokens"
                        ):
                            truncated = True
                text = "".join(parts)
            else:
                response = await provider.complete(prompt, system=effective_system)
                text = response.text
                input_tokens = response.input_tokens
                output_tokens = response.output_tokens
                # getattr keeps the old duck-typed _FakeResponse test
                # fixtures (which never carried truncation metadata)
                # working — they default to "not truncated."
                truncated = getattr(response, "truncated", False)
            duration = time.perf_counter() - start
            # 0.1.26 — truncation caps success_score at 0.5. A mid-
            # sentence answer is not a successful attempt; it's a
            # signal to the deliberation loop to keep going (or to
            # the retry machinery to try another citizen). Without
            # this cap the judge happily gave 100% to a 6-line list
            # that ended on "MIT CSAIL: https://csail.mit.edu — MIT's
            # Computer" — clearly mid-thought.
            from anthill.core.failure import FailureReason, classify_attempt
            if text.strip() and not truncated:
                success_score = 1.0
            elif truncated:
                # Some signal is better than none, but pheromone
                # deposit should reflect "this wasn't done."
                success_score = 0.5
            else:
                success_score = 0.0
            reason = classify_attempt(
                text, exception=None, success_score=success_score
            )
            # Truncation wins over whatever classify_attempt produced
            # from the body text — a length-stopped response isn't a
            # NETWORK / MODEL_ERROR / AUTH / POLICY case.
            if truncated:
                reason = FailureReason.TRUNCATED
            # 0.1.40 — if the citizen punted the work back to the
            # king ("please paste the content"), treat it as a
            # zero-score failure so the executor's retry path kicks
            # in. The next attempt picks a different citizen AND
            # (via core/refusal.RESOURCEFUL_RETRY_ADDENDUM, applied
            # at the executor layer) gets a "be more resourceful"
            # nudge. Anthill's core narrative: citizens serve the
            # king, they don't bounce work back.
            if reason == FailureReason.USER_SERVING_REFUSAL:
                success_score = 0.0
            return TaskResult(
                task_id=task_id,
                agent_id=self.id,
                task_type=task_type,
                output=text,
                success_score=success_score,
                duration_seconds=duration,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                failure_reason=reason.value if reason is not None else None,
                truncated=truncated,
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
