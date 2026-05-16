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

from pathlib import Path

from anthill.core.agent import Agent, TaskResult
from anthill.core.culture import Culture
from anthill.core.episodic import find_similar, format_similar_for_scout
from anthill.core.judge import judge_enabled, judge_output
from anthill.core.workflows import format_templates_for_scout, load_workflows
from anthill.core.executor import ProgressEvent, SubtaskOutcome, execute_plan
from anthill.core.inflight import (
    CompletedStep,
    InflightAsk,
    clear_inflight,
    save_inflight,
)
from anthill.core.pheromone import PheromoneTrail
from anthill.core.plan_cache import CachedPlan, lookup as cache_lookup, remember as cache_remember
from anthill.core.router import Router, RouterConfig
from anthill.core.scout import Plan, Scout


@dataclass
class AskResult:
    """The aggregated outcome of a natural-language request.

    A single user request may produce one or many subtask outcomes. Each
    outcome carries every attempt that was made (success or retry), the
    final status, and the result the user-facing output should draw on.
    `final_output` surfaces what the king almost certainly came for —
    the synthesis step at the end of the chain.
    """

    request: str
    plan: Plan
    outcomes: list[SubtaskOutcome]
    ask_id: str | None = None  # filled when checkpoint was active

    @property
    def final_output(self) -> str:
        """The output of the last subtask — by convention, the synthesis step.

        If the last subtask failed or was skipped, we walk backward until
        we find a step that did produce something — better to show partial
        progress than an opaque error.
        """
        for outcome in reversed(self.outcomes):
            if outcome.status == "ok":
                return outcome.output
        # Nothing succeeded.
        return "[No subtask completed successfully.]"

    @property
    def succeeded(self) -> bool:
        """True only when every subtask reached status 'ok'."""
        return all(o.status == "ok" for o in self.outcomes)

    @property
    def results(self) -> list[TaskResult]:
        """Backwards-compatible flat view of final attempts (skipped -> None filtered).

        Older callers expected a list[TaskResult]; we still surface that
        view, but new code should iterate outcomes for full retry traces.
        """
        return [o.final for o in self.outcomes if o.final is not None]


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
    plan_cache: dict[str, CachedPlan] = field(default_factory=dict)
    last_ask_cache_hit: bool = field(default=False, repr=False)
    # Path to history.jsonl so ask() can pull similar past examples.
    history_path: Path | None = field(default=None, repr=False)
    # Judge config (LLM-based quality scoring).
    use_judge: bool = field(default_factory=judge_enabled)
    judge_model: str = "deepseek-chat"

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

    async def run(
        self,
        task_type: str,
        prompt: str,
        *,
        forbid: set[str] | None = None,
    ) -> TaskResult:
        """Execute one typed task: route, run, judge, deposit pheromone.

        `forbid` lets a caller exclude specific citizens — typically used on
        retry to avoid the citizen that just failed.

        When use_judge is true, the LLM judge replaces the worker's binary
        success score with a [0, 1] quality score. Pheromones now reinforce
        quality rather than mere liveness.
        """
        agent = self.router.assign(task_type, forbid=forbid)
        result = await agent.execute(task_type, prompt, system=self._compose_system(agent))

        if self.use_judge and result.success_score > 0:
            verdict = await judge_output(prompt, str(result.output), model=self.judge_model)
            result.success_score = verdict.score

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

    async def ask(
        self,
        request: str,
        *,
        on_progress=None,
        resume: InflightAsk | None = None,
        nation_dir: Path | None = None,
    ) -> AskResult:
        """Execute a natural-language request from the king.

        The Scout decomposes the request into typed subtasks; each subtask
        runs through the normal pheromone-routed pipeline. Dependencies are
        respected by executing subtasks sequentially in plan order (a real
        DAG executor can replace this when subtasks need to run in parallel).

        The nation's existing task-type vocabulary is fed to Scout so it
        prefers reusing established labels — keeping pheromone trails
        concentrated instead of fragmenting them into one-shot categories.

        on_progress: optional async callback receiving ProgressEvent
        objects as each subtask starts/retries/finishes. Use it to drive
        live UI; pass None for headless callers.

        resume: when provided, treat that InflightAsk's completed steps
        as already done; only the missing subtasks run. The plan from
        the resume payload is used as-is (no re-planning) so that an
        ask is reproducible across restarts.

        nation_dir: where to write the in-flight checkpoint file. When
        omitted, no checkpoint is written — useful for tests and for
        callers that do their own persistence.
        """
        if resume is not None:
            plan = resume.plan
            self.last_ask_cache_hit = False
            inflight = resume
        else:
            cached = cache_lookup(request, self.plan_cache)
            if cached is not None:
                plan = cached.plan
                self.last_ask_cache_hit = True
            else:
                similar_block = self._similar_past_block(request)
                workflow_block = self._workflow_templates_block()
                episodic_context = "\n\n".join(b for b in (workflow_block, similar_block) if b)
                scout = Scout(model=self.scout_model)
                plan = await scout.plan(
                    request,
                    known_task_types=self.culture.known_task_types(),
                    episodic_context=episodic_context,
                )
                cache_remember(request, plan, self.plan_cache)
                self.last_ask_cache_hit = False
            inflight = InflightAsk.new(request=request, plan=plan) if nation_dir else None

        # Pre-seed the executor with already-completed steps (resume only).
        resume_state: dict[int, SubtaskOutcome] | None = None
        if resume is not None and resume.completed:
            resume_state = {}
            for step in resume.completed:
                if step.index < 0 or step.index >= len(plan.subtasks):
                    continue  # plan/step mismatch — ignore the dangling entry
                synthetic = TaskResult(
                    task_id=f"resume-{step.index}",
                    agent_id=step.agent_id,
                    task_type=step.task_type,
                    output=step.output,
                    success_score=step.success_score,
                    duration_seconds=step.duration_seconds,
                    input_tokens=step.input_tokens,
                    output_tokens=step.output_tokens,
                )
                outcome = SubtaskOutcome(
                    subtask=plan.subtasks[step.index],
                    attempts=[synthetic],
                    status="ok",
                    started_at=step.started_at,
                    ended_at=step.ended_at,
                )
                resume_state[step.index] = outcome

        # Write the initial checkpoint before any work starts so a crash
        # during the very first subtask still leaves something resumable.
        if nation_dir is not None and inflight is not None:
            save_inflight(inflight, nation_dir)

        async def _checkpointing_progress(event: ProgressEvent) -> None:
            if on_progress is not None:
                await on_progress(event)
            if (
                inflight is None
                or nation_dir is None
                or event.kind != "finished"
                or event.outcome.status != "ok"
                or event.outcome.final is None
            ):
                return
            final = event.outcome.final
            inflight.record_completed(
                CompletedStep(
                    index=event.index,
                    task_type=event.subtask.task_type,
                    output=str(final.output),
                    agent_id=final.agent_id,
                    started_at=event.outcome.started_at or 0.0,
                    ended_at=event.outcome.ended_at or 0.0,
                    attempts=len(event.outcome.attempts),
                    success_score=final.success_score,
                    input_tokens=final.input_tokens,
                    output_tokens=final.output_tokens,
                )
            )
            save_inflight(inflight, nation_dir)

        progress_cb = _checkpointing_progress if inflight is not None else on_progress
        outcomes = await execute_plan(
            plan,
            self,
            on_progress=progress_cb,
            resume_state=resume_state,
        )

        # Whole ask done — drop the checkpoint regardless of per-subtask
        # status. Resume is for "didn't reach the cleanup", not "retry
        # failures within an ask that ran to completion".
        if nation_dir is not None and inflight is not None:
            clear_inflight(inflight.ask_id, nation_dir)

        return AskResult(
            request=request,
            plan=plan,
            outcomes=outcomes,
            ask_id=inflight.ask_id if inflight is not None else None,
        )

    def _similar_past_block(self, request: str) -> str:
        """Pull a small context block of similar past asks, if history is available."""
        if self.history_path is None or not self.history_path.exists():
            return ""
        from anthill.core.history import load_history
        entries = load_history(self.history_path.parent, limit=200)
        if not entries:
            return ""
        hits = find_similar(request, entries, top_k=3, min_score=0.2)
        return format_similar_for_scout(hits)

    def _workflow_templates_block(self) -> str:
        """Context block listing this nation's known workflow shapes."""
        if self.history_path is None:
            return ""
        templates = load_workflows(self.history_path.parent)
        return format_templates_for_scout(templates, top_k=5)
