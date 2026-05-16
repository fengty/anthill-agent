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

import time
from dataclasses import dataclass, field

from pathlib import Path

from anthill.core.agent import Agent, TaskResult
from anthill.core.budget import Budget, BudgetSnapshot, BudgetTracker, snapshot
from anthill.core.cost_signal import (
    COST_DIMENSION,
    compute_cost_usd,
    cost_efficiency,
    update_baseline,
)
from anthill.core.culture import Culture
from anthill.core.immune import (
    CitizenHealth,
    maybe_probe_release,
    maybe_quarantine,
    record_attempt,
)
from anthill.core.values import DimensionCatalog
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
from anthill.core.scout import Plan, Scout, Subtask


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
    budget: BudgetSnapshot | None = None  # filled when a Budget was set
    replans: int = 0  # number of self-correction passes that ran

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
    # Open-vocabulary quality dimensions the nation has accumulated from
    # judge verdicts + user `anthill rate --dim` calls. Empty by default;
    # the LLM judge invents dimensions over time.
    dimension_catalog: DimensionCatalog = field(default_factory=DimensionCatalog)
    # Per-task_type rolling baseline USD cost. Each attempt updates the
    # baseline via EWMA; cost-efficiency for a single attempt is computed
    # by comparing to this. v0.4.3+ — see core/cost_signal.
    cost_baselines: dict[str, float] = field(default_factory=dict)
    # Immune system (v0.5+). Sliding windows of recent attempts per
    # citizen. In-memory only — rebuilt from history on load if desired.
    citizen_health: dict[str, CitizenHealth] = field(
        default_factory=dict, repr=False
    )
    # Auto-quarantine pipeline gate. Default OFF: the user has to opt in
    # via `anthill citizen quarantine policy --auto on` or by setting
    # this directly. Manual `anthill citizen quarantine <id>` always
    # works regardless.
    immune_enabled: bool = False
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

    def find_agent(self, agent_id: str) -> Agent | None:
        """Locate by full or prefix id. Returns the first match or None."""
        for a in self.agents:
            if a.id == agent_id or a.id.startswith(agent_id):
                return a
        return None

    def alive_agents(self) -> list[Agent]:
        """Active (non-retired) citizens — the working population."""
        return [a for a in self.agents if not a.is_retired]

    def retire(self, agent_id: str) -> Agent | None:
        """Mark a citizen retired so the router stops assigning to it.

        Returns the agent that was retired, or None if not found / already
        retired. Idempotent: re-retiring is a no-op rather than an error,
        so a CLI that runs `retire` over a partial selection can safely
        cover ground without checking each id first.
        """
        a = self.find_agent(agent_id)
        if a is None or a.is_retired:
            return None
        a.retired_at = time.time()
        return a

    def unretire(self, agent_id: str) -> Agent | None:
        """Restore a retired citizen to active duty."""
        a = self.find_agent(agent_id)
        if a is None or not a.is_retired:
            return None
        a.retired_at = None
        return a

    def quarantine(self, agent_id: str, reason: str = "manual") -> Agent | None:
        """Manually quarantine a citizen. Idempotent."""
        a = self.find_agent(agent_id)
        if a is None or a.is_quarantined:
            return None
        a.quarantined_at = time.time()
        a.quarantine_reason = reason
        # Reset probe streak so the next observations decide cleanly.
        health = self.citizen_health.get(a.id)
        if health is not None:
            health.probe_streak = 0
        return a

    def unquarantine(self, agent_id: str) -> Agent | None:
        """Manually release a citizen from quarantine."""
        a = self.find_agent(agent_id)
        if a is None or not a.is_quarantined:
            return None
        a.quarantined_at = None
        a.quarantine_reason = None
        health = self.citizen_health.get(a.id)
        if health is not None:
            health.probe_streak = 0
        return a

    @property
    def router(self) -> Router:
        return Router(
            self.pheromones,
            self.agents,
            self.router_config,
            dim_weights=dict(self.dimension_catalog.weights),
        )

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
            # Whichever dimensions the judge invented (or used) get
            # mirrored onto the TaskResult. The catalog auto-registers
            # them — Anthill is the mechanism; the LLM decides what
            # "good" means for this nation's work.
            if verdict.scores:
                result.scores.update(verdict.scores)
                for dim_name, score in verdict.scores.items():
                    self.dimension_catalog.observe(
                        dim_name,
                        score=score,
                        description=verdict.explanations.get(dim_name, ""),
                    )

        self.pheromones.deposit(
            agent_id=result.agent_id,
            task_type=result.task_type,
            success_score=result.success_score,
        )
        # If we got per-dimension scores, fold them into the trail too so
        # future routing decisions can ask "who scores well on X here?"
        if result.scores:
            self.pheromones.record_dimensions(
                result.agent_id, result.task_type, result.scores
            )
        # Cost-efficiency as an open-vocabulary dimension (v0.4.3). The
        # router won't use it until the user sets a weight on `cost` via
        # `anthill values weight cost ...` — so the default behavior is
        # unchanged. What v0.4.3 supplies is the *signal*, not the policy.
        cost = compute_cost_usd(
            result.input_tokens, result.output_tokens, agent.model
        )
        baseline_before = self.cost_baselines.get(task_type)
        efficiency = cost_efficiency(cost, baseline_before)
        update_baseline(self.cost_baselines, task_type, cost)
        if cost > 0 or baseline_before is not None:
            # Skip when both this attempt and the baseline are zero
            # (e.g. fake providers in tests). No signal worth recording.
            result.scores[COST_DIMENSION] = efficiency
            self.pheromones.record_dimensions(
                result.agent_id, result.task_type, {COST_DIMENSION: efficiency}
            )
            self.dimension_catalog.observe(
                COST_DIMENSION,
                score=efficiency,
                description="cost-efficiency relative to recent baseline for this task type",
            )
        # The catalog records every attempted task, not just successful ones —
        # the nation's vocabulary is the work it tries, not only what it
        # succeeds at.
        self.culture.record(task_type)

        # Immune system (v0.5): update health window + maybe quarantine.
        # Always update the window regardless of immune_enabled so a
        # later "turn it on" has data to work with. The maybe_quarantine
        # / maybe_probe_release calls themselves check the flag.
        health = record_attempt(self, result.agent_id, task_type, result)
        if agent.is_quarantined:
            maybe_probe_release(self, agent, health, result)
        else:
            maybe_quarantine(self, agent, health)
        return result

    def _model_for_agent(self, agent_id: str) -> str:
        """Look up the model name for an agent_id. 'unknown' if not found."""
        for a in self.agents:
            if a.id == agent_id:
                return a.model
        return "unknown"

    async def ask(
        self,
        request: str,
        *,
        on_progress=None,
        resume: InflightAsk | None = None,
        nation_dir: Path | None = None,
        budget: Budget | None = None,
        max_replans: int = 1,
        pre_plan: Plan | None = None,
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
        elif pre_plan is not None:
            # Recipe path: skip Scout entirely, use the user-supplied plan
            # as-is. The cache is bypassed too — a recipe-run is the user
            # saying "do exactly this," not "let the nation negotiate."
            plan = pre_plan
            self.last_ask_cache_hit = False
            inflight = InflightAsk.new(request=request, plan=plan) if nation_dir else None
        else:
            cached = cache_lookup(request, self.plan_cache)
            if cached is not None:
                plan = cached.plan
                self.last_ask_cache_hit = True
            else:
                similar_block = self._similar_past_block(request)
                workflow_block = self._workflow_templates_block()
                plugin_block = self._plugin_stats_block()
                episodic_context = "\n\n".join(
                    b for b in (workflow_block, plugin_block, similar_block) if b
                )
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
        tracker: BudgetTracker | None = None
        if budget is not None and not budget.is_empty():
            tracker = BudgetTracker(budget, model_lookup=self._model_for_agent)
        outcomes = await execute_plan(
            plan,
            self,
            on_progress=progress_cb,
            resume_state=resume_state,
            budget=tracker,
        )

        # Self-correction loop. When a subtask fails terminally, ask Scout
        # to salvage the plan around it and re-execute. Capped at
        # max_replans so a chronically broken request can't loop forever.
        # Skipped when a budget cap blew — replanning would just burn more
        # budget on the same dead end.
        replans_done = 0
        while (
            replans_done < max_replans
            and any(o.status == "failed" for o in outcomes)
            and (tracker is None or tracker.may_run_next() is None)
        ):
            replan_result = await self._try_replan(
                request, plan, outcomes, on_progress=progress_cb, budget=tracker
            )
            if replan_result is None:
                break
            plan, outcomes = replan_result
            replans_done += 1

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
            budget=snapshot(tracker) if tracker is not None else None,
            replans=replans_done,
        )

    async def _try_replan(
        self,
        request: str,
        plan: Plan,
        outcomes: list[SubtaskOutcome],
        *,
        on_progress,
        budget: BudgetTracker | None,
    ) -> tuple[Plan, list[SubtaskOutcome]] | None:
        """One self-correction pass.

        Identifies the first failed subtask, asks Scout for a salvage
        sub-plan, splices it in after the already-OK steps, and
        re-executes only the spliced-in subtasks (the prior ones come
        through as resume_state). Returns the new (plan, outcomes) on
        success, or None when there is nothing useful to do — caller
        keeps the original outcomes in that case.
        """
        first_failed = next(
            (i for i, o in enumerate(outcomes) if o.status == "failed"),
            None,
        )
        if first_failed is None:
            return None

        failed_outcome = outcomes[first_failed]
        succeeded_pairs: list[tuple[Subtask, str]] = [
            (plan.subtasks[i], outcomes[i].output)
            for i in range(first_failed)
            if outcomes[i].status == "ok"
        ]
        remaining = list(plan.subtasks[first_failed + 1 :])
        failure_reason = (
            f"after {len(failed_outcome.attempts)} attempt(s); "
            "every available citizen returned an empty or error response"
        )

        scout = Scout(model=self.scout_model)
        try:
            salvage = await scout.replan(
                request,
                succeeded=succeeded_pairs,
                failed=plan.subtasks[first_failed],
                failure_reason=failure_reason,
                remaining=remaining,
                known_task_types=self.culture.known_task_types(),
            )
        except Exception:  # noqa: BLE001 — replan is best-effort
            return None
        if salvage is None or not salvage.subtasks:
            return None

        # Build the new full plan: kept-OK subtasks + salvage subtasks.
        kept_subtasks = [plan.subtasks[i] for i in range(first_failed)
                         if outcomes[i].status == "ok"]
        new_subtasks = kept_subtasks + list(salvage.subtasks)
        new_plan = Plan(subtasks=new_subtasks)

        # Resume state covers the kept subtasks at their new indices.
        new_resume: dict[int, SubtaskOutcome] = {}
        for new_idx, old_idx in enumerate(
            i for i in range(first_failed) if outcomes[i].status == "ok"
        ):
            new_resume[new_idx] = outcomes[old_idx]

        new_outcomes = await execute_plan(
            new_plan,
            self,
            on_progress=on_progress,
            resume_state=new_resume,
            budget=budget,
        )
        return new_plan, new_outcomes

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

    def _plugin_stats_block(self) -> str:
        """v0.7.2 — plugin usage summary fed into Scout's planning context.

        Without this, Scout proposes plugin usage based only on its
        prompt and the plugin description. With it, Scout sees evidence
        of what has actually worked for THIS nation — closing the loop
        between past plugin telemetry and future plan choices.
        """
        if self.history_path is None:
            return ""
        from anthill.core.plugin_usage import (
            aggregate_usage,
            format_plugin_stats_for_scout,
            load_plugin_usage,
        )
        records = load_plugin_usage(self.history_path.parent)
        if not records:
            return ""
        return format_plugin_stats_for_scout(aggregate_usage(records))
