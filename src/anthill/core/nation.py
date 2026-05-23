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
from anthill.models.base import ModelProvider
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


# 0.2.9 — brevity directive prepended to every citizen's system prompt.
#
# Why: real-session data (sess-173c98b13a.jsonl) showed citizens
# defaulting to 8 KB tutorials for 5-char questions. The 30+ second
# response wasn't network latency — it was the model writing pages
# of tables and step-by-step guides nobody asked for. Output length
# is by far the biggest controllable contributor to felt slowness.
#
# Directive shape: ONE paragraph, no bullets (bullets would invite
# the model to mirror that structure). Triggers on EVERY subtask
# unless the user's request explicitly says "详细" / "完整" / "step
# by step" / "tell me everything" — in which case the model is
# allowed to go long.
_BREVITY_DIRECTIVE = """\
Default to concise outputs: aim for under 800 characters and 1-2
sections. Concrete commands / code / examples beat prose. Skip
preamble ("好的，让我帮您...") and skip wrap-up summaries
("综上所述..."). End with "想展开告诉我" or equivalent if the user
might want more. ONLY produce long form (multiple sections, tables,
step-by-step guides) when the user explicitly says "详细" /
"完整" / "step by step" / "tell me everything". This applies to
every response; if a longer answer is genuinely needed, lead with
the answer and put the elaboration AFTER, not before."""


@dataclass
class AskTimings:
    """0.1.44 — per-ask wall-clock breakdown for diagnostics.

    Why this exists: until 0.1.44 we only logged a single `duration`
    per turn, so when an ask ran 45-103s we had no way to tell which
    phase ate the time — Scout planning, a slow subtask, refusal-
    retry, or something else. This dataclass captures each phase so
    the REPL can print `[14.8s — Scout 3.1s, research 6.4s, analyze
    5.3s]` and the session JSONL preserves the breakdown for later
    analysis without re-running the ask.

    Wall-clock semantics: `total_seconds` is the full `ask()` span.
    `scout_seconds` is just the Scout LLM call (None when Scout was
    bypassed — cache hit, trivial fast-path, skill match, pre-plan,
    or resume). Per-subtask times come from each outcome's
    started_at / ended_at and reflect wall-clock per subtask
    (parallel subtasks within a wave will overlap, so summing them
    overshoots `total_seconds` by design — that's how the user can
    SEE parallelism happened).

    0.1.47 added `clarify_seconds` after a real user-reported case
    where total=11.8s but Scout(1.5s)+general(2.9s)=4.4s only — the
    missing 7.4s turned out to be `maybe_clarify` running invisibly.
    """

    total_seconds: float = 0.0
    scout_seconds: float | None = None
    # 0.1.47 — `maybe_clarify` round-trip when on_clarify is wired
    # AND the request isn't trivial. None when clarify was skipped.
    clarify_seconds: float | None = None
    # (task_type, wall_clock_seconds) per subtask, in plan order.
    subtask_seconds: list[tuple[str, float]] = field(default_factory=list)
    # Count of attempts whose failure_reason was user_serving_refusal —
    # the 0.1.40 retry-with-resourceful-nudge path. Useful for
    # debugging "why did this ask take so long" — refusal-retries
    # always add latency.
    refusal_retry_count: int = 0
    # Bypass marker: which fast path was taken, if any. One of
    # "scout", "cache", "trivial", "skill", "pre_plan", "resume".
    # Lets the timing line show "📋 (skill)" so you know WHY it was
    # fast without having to remember the matching rules.
    plan_source: str = "scout"

    def to_dict(self) -> dict:
        """Serialize for session JSONL. Forward-compatible: older
        readers that don't know about this key will just ignore it."""
        return {
            "total_seconds": round(self.total_seconds, 3),
            "scout_seconds": (
                round(self.scout_seconds, 3)
                if self.scout_seconds is not None
                else None
            ),
            "clarify_seconds": (
                round(self.clarify_seconds, 3)
                if self.clarify_seconds is not None
                else None
            ),
            "subtask_seconds": [
                [tt, round(s, 3)] for tt, s in self.subtask_seconds
            ],
            "refusal_retry_count": self.refusal_retry_count,
            "plan_source": self.plan_source,
        }


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
    # 0.1.44 — wall-clock breakdown for "[14.8s — Scout 3.1s …]".
    # Filled by Nation.ask() before return. Older callers that
    # never read this still work; new REPL displays it.
    timings: AskTimings = field(default_factory=AskTimings)
    # 0.1.4+ — history entries Scout actually borrowed from when planning.
    # Surfaced by the REPL as "📚 borrowed from: id1, id2" so the user can
    # SEE the nation's memory working, not just trust the readme. Empty
    # when the cache was a hit / no similar past existed / Scout was
    # bypassed (trivial fast path).
    episodic_sources: list[str] = field(default_factory=list)
    # 0.1.13+ — set to True if the user cancelled the plan review.
    # `outcomes` will be empty and `final_output` returns "". The REPL
    # surfaces this distinctly from "no subtasks succeeded" so the user
    # sees that nothing ran (vs. everything failed).
    cancelled_by_user: bool = False

    @property
    def final_output(self) -> str:
        """The output of the last subtask — by convention, the synthesis step.

        If the last subtask failed or was skipped, we walk backward until
        we find a step that did produce something — better to show partial
        progress than an opaque error.
        """
        # 0.1.13 — user-cancelled plans have no work to surface. Return
        # empty so the REPL doesn't paste "[No subtask completed
        # successfully.]" for a deliberate cancel.
        if self.cancelled_by_user:
            return ""
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
    # 0.1.42 — skill-first plan lookup. When the most recent ask
    # was served by a saved recipe (instead of Scout planning),
    # this holds the SkillMatch so the REPL can render "📚 using
    # skill X" and the post-ask hook can skip the distillation
    # prompt (we already had a skill for this).
    last_matched_skill: object = field(default=None, repr=False)
    # 0.1.29 — persistent memory injection. Composed by the REPL / CLI
    # at session start from USER.md (global) + MEMORY.md (per-nation),
    # then appended to every Scout + worker system prompt by
    # _compose_system. Empty string means "no memory yet" — perfectly
    # fine for headless tests / first-run.
    memory_context: str = field(default="", repr=False)
    # 0.1.33 — project-context injection mode. "auto" (default) uses
    # is_project_relevant_request per ask. "on" forces injection.
    # "off" disables it. REPL writes this from SessionStats before
    # each ask via /project on/off/auto.
    project_inject_mode: str = field(default="auto", repr=False)
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
        """Combine brevity directive + agent persona + nation house_style +
        persistent memory.

        0.2.9 — brevity directive added at the TOP. Real-session data
        showed citizens default to wall-of-text outputs (8 KB tutorials
        with multiple sections, tables, error-troubleshooting matrices)
        even when the user asked a 2-sentence question. The 32-second
        wait that follows isn't network or model latency — it's the
        model GENERATING those extra 7 KB. Cap the default; let the
        user opt into long form.

        0.2.18 — when `_in_loop_iteration` is set (the REPL's /loop
        runner toggles this around each ask), we:
          - SKIP the brevity directive (its "end with 想展开告诉我"
            fights the loop's "end with [[loop:...]]" marker contract)
          - APPEND the loop marker contract as a system-level postlude
            (system prompt has more authority than appending to the
            user request — which is what 0.2.2 did and what real-
            session data showed the model dropping)

        Persona is the agent's individual disposition. House style is
        the nation's shared voice. Memory context (0.1.29+) is the
        union of USER.md (what the king has told us about themselves)
        and MEMORY.md (what this nation has learned). All apply at
        once.

        Order matters: brevity first (it sets length defaults that
        other parts may override locally), then memory so the agent
        sees user preferences BEFORE persona/style. Persona quirks
        shouldn't override what the user explicitly asked for.
        """
        parts: list[str] = []
        # 0.2.27 — identity preamble at the absolute top, BEFORE
        # brevity. User feedback showed deepseek emitting "我没有
        # shell 访问权限" despite SHELL_TOOL_INSTRUCTION being in
        # the prompt. The fix is identity: ANTHILL IS AN AGENT THAT
        # ACTS. Suppressed only when /noexec is on (no shell/browser
        # means the "you can act" claim would be a lie).
        if not getattr(self, "_exec_disabled", False):
            from anthill.core.shell import AGENT_IDENTITY_PREAMBLE
            parts.append(AGENT_IDENTITY_PREAMBLE.strip())
        in_loop = bool(getattr(self, "_in_loop_iteration", False))
        if not in_loop:
            parts.append(_BREVITY_DIRECTIVE.strip())
        if self.memory_context.strip():
            parts.append(self.memory_context.strip())
        if agent.persona:
            parts.append(agent.persona.strip())
        style = self.culture.house_style.strip() if self.culture.house_style else ""
        if style:
            parts.append("Nation house style:\n" + style)
        # 0.2.19 — shell tool. Every citizen sees the [[bash:CMD]]
        # marker contract so they emit commands when the king asks
        # for actual local action instead of explaining what to type.
        # Suppressed when `_exec_disabled` is True (set by /noexec).
        if not getattr(self, "_exec_disabled", False):
            from anthill.core.shell import SHELL_TOOL_INSTRUCTION
            parts.append(SHELL_TOOL_INSTRUCTION.strip())
            # 0.2.26 — browser tool for functional UI testing.
            # Pairs with [[bash:]] — bash for "run a command",
            # browser for "drive a webpage". Same /noexec gate.
            from anthill.core.browser_drive import BROWSER_TOOL_INSTRUCTION
            parts.append(BROWSER_TOOL_INSTRUCTION.strip())
        if in_loop:
            # Import locally to avoid a circular import at module
            # load (loop imports from agent → agent imports from
            # nation in some test paths).
            from anthill.core.loop import SELF_PACE_INSTRUCTION
            parts.append(SELF_PACE_INSTRUCTION.strip())

        # 0.2.28 — for model families that regress to chatbot mode
        # (deepseek/glm/gpt/minimax), append a short tail reinforcement
        # so the LAST thing in context is "act, don't describe."
        # Recency bias helps where front-loading alone failed.
        if not getattr(self, "_exec_disabled", False):
            from anthill.core.shell import (
                TOOL_USE_REINFORCEMENT_TAIL,
                model_needs_strong_tool_reinforcement,
            )
            if model_needs_strong_tool_reinforcement(agent.model):
                parts.append(TOOL_USE_REINFORCEMENT_TAIL.strip())

        return "\n\n".join(parts) or None

    async def run(
        self,
        task_type: str,
        prompt: str,
        *,
        forbid: set[str] | None = None,
        on_token=None,
        on_tool_call=None,
        on_tool_result=None,
    ) -> TaskResult:
        """Execute one typed task: route, run, judge, deposit pheromone.

        `forbid` lets a caller exclude specific citizens — typically used on
        retry to avoid the citizen that just failed.

        ``on_token`` (v0.1.10+) is an optional async callback that
        receives each incremental text delta as the provider produces
        it. When set, the agent uses the provider's streaming API; the
        cumulative text matches the non-streaming path.

        ``on_tool_call`` / ``on_tool_result`` (0.2.30) fire during the
        native tool_use agent loop. Used by the REPL to render
        "🐚 running: cmd" / output panels inline as the loop unfolds.

        When use_judge is true, the LLM judge replaces the worker's binary
        success score with a [0, 1] quality score. Pheromones now reinforce
        quality rather than mere liveness.
        """
        agent = self.router.assign(task_type, forbid=forbid)
        # 0.2.30 — opt into agent loop via nation.agentic_mode.
        # Default OFF: existing flow (single-shot + [[bash:]] markers)
        # stays unchanged. /agentic on flips citizens into native
        # tool_use multi-turn mode. Will become default in 0.2.32+
        # once the provider matrix is fully validated.
        use_loop = (
            getattr(self, "agentic_mode", False)
            and not getattr(self, "_exec_disabled", False)
            # Provider must implement native tool_use. Old duck-typed
            # _FakeProvider in tests doesn't — falls back cleanly.
            and hasattr(agent._get_provider(), "complete_with_messages")
            and type(agent._get_provider()).complete_with_messages
                is not ModelProvider.complete_with_messages
        )
        # 0.2.31 — wire kanban-aware dispatch when the caller passed
        # a home dir on the nation. 0.2.32 — also pass `nation=self`
        # so the dispatch can register delegate_task for multi-agent
        # collaboration. Falls back to default dispatch (no kanban,
        # no delegate) when no home is bound — keeps tests / headless
        # callers working without filesystem state.
        agent_executor = None
        if use_loop:
            home = getattr(self, "_anthill_home", None)
            if home is not None:
                from anthill.core.tool_executors import (
                    make_dispatch_with_kanban,
                )
                agent_executor = make_dispatch_with_kanban(
                    home,
                    default_assignee=agent.id,
                    nation=self,  # 0.2.32: enables delegate_task
                )
        result = await agent.execute(
            task_type,
            prompt,
            system=self._compose_system(agent),
            on_token=on_token,
            use_agent_loop=use_loop,
            agent_loop_executor=agent_executor,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
        )

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
        on_clarify=None,
        on_plan=None,
        on_tool_call=None,
        on_tool_result=None,
        resume: InflightAsk | None = None,
        nation_dir: Path | None = None,
        budget: Budget | None = None,
        max_replans: int = 1,
        pre_plan: Plan | None = None,
        forbid: set[str] | None = None,
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

        on_clarify: optional async callback to handle clarification turns
        (v0.9.0+). When provided AND the request isn't trivial, the
        nation runs a quick clarifier; if it flags ambiguity, the
        callback is invoked with the questions and should return the
        user's answer (or None to skip). Skipped for resume / pre_plan
        paths since those already carry a fixed plan.
        """
        # 0.1.44 — capture wall-clock from the moment we accept the
        # ask. `perf_counter` is monotonic so it survives clock jumps;
        # we only need DELTAS, not absolute times.
        ask_started_perf = time.perf_counter()
        timings = AskTimings()

        # v0.9.0 — clarification turn. Only fires when:
        #   1. Caller registered an on_clarify handler
        #   2. Not on resume / pre_plan paths (plan is already locked)
        #   3. fast_classify doesn't already say "trivial" (greetings
        #      shouldn't trigger a clarifier round trip)
        if (
            on_clarify is not None
            and resume is None
            and pre_plan is None
        ):
            from anthill.core.clarify import maybe_clarify
            from anthill.core.complexity import fast_classify
            should_clarify = fast_classify(request) != "trivial"
            # 0.2.5 — additional skip when the request is already
            # substantive. Real-session data showed clarify burning
            # 22.8s on "mysql这些" (5 chars) while the user knew
            # what they wanted — and burning ~3.6s on "你能做什么"
            # (5 chars) before producing a generic answer.
            # Heuristic: skip clarify when the ask already contains
            # >=25 chars of substance. Clarify is most useful for
            # genuinely ambiguous fragments; once the user typed a
            # sentence they've committed to a direction.
            if should_clarify and len(request.strip()) >= 25:
                should_clarify = False
            if should_clarify:
                # 0.1.47 — capture clarify wall-clock. This was the
                # 7.4s "hidden" cost in real session logs.
                _clarify_t0 = time.perf_counter()
                request = await maybe_clarify(self, request, on_clarify)
                timings.clarify_seconds = time.perf_counter() - _clarify_t0

        # Track which history entries Scout actually read, for the REPL
        # "📚 borrowed from" line. Stays empty on resume / pre_plan /
        # cache-hit / trivial-fast paths where Scout was bypassed.
        episodic_sources: list[str] = []

        if resume is not None:
            plan = resume.plan
            self.last_ask_cache_hit = False
            inflight = resume
            timings.plan_source = "resume"
        elif pre_plan is not None:
            # Recipe path: skip Scout entirely, use the user-supplied plan
            # as-is. The cache is bypassed too — a recipe-run is the user
            # saying "do exactly this," not "let the nation negotiate."
            plan = pre_plan
            self.last_ask_cache_hit = False
            inflight = InflightAsk.new(request=request, plan=plan) if nation_dir else None
            timings.plan_source = "pre_plan"
        else:
            # 0.1.42 — skill-first plan lookup. Before letting Scout
            # regenerate the same plan shape for a recurring request,
            # search the nation's saved recipes for a match. Matches
            # the "用户是国王，子民先查家底" philosophy: ask comes in,
            # citizens FIRST check what's already known how to do.
            skill_match = None
            if nation_dir is not None:
                try:
                    from anthill.core.skill_match import find_matching_skill
                    skill_match = find_matching_skill(request, nation_dir)
                except Exception:  # noqa: BLE001 — skill lookup must never block
                    skill_match = None
            cached = cache_lookup(request, self.plan_cache)
            if skill_match is not None and skill_match.recipe.subtasks:
                # Found a saved skill — use its subtask list directly,
                # bypassing Scout. Equivalent to the pre_plan path but
                # auto-chosen, with the skill recorded for the REPL
                # to surface via "📚 using skill X".
                #
                # 0.1.69 — extract {url}/{id}/{date} from THIS ask and
                # substitute into the recipe's templates before
                # handing to citizens. Before this fix, the literal
                # placeholder string ("{url}") was passed verbatim,
                # and citizens correctly complained that "{url}"
                # isn't a URL. Saved skills were dead.
                from anthill.core.skill_match import extract_variables
                args = extract_variables(request)
                try:
                    filled = skill_match.recipe.fill(args)
                except KeyError:
                    # A placeholder in the template wasn't extractable
                    # from the new request (e.g. recipe has {date} but
                    # request has no ISO date). Fall back to using the
                    # original ask request — at least the citizen gets
                    # something it can read.
                    filled = None
                if filled is not None and filled.plan is not None:
                    plan = filled.plan
                    # Preserve complexity hint from the recipe (defaults
                    # to "normal" the same as before).
                    plan.complexity = "normal"
                else:
                    # Defensive fallback (extraction missed a placeholder
                    # OR recipe had no subtasks list). Use the raw
                    # request as a single general subtask. This keeps
                    # the ask from dying when the recipe schema is
                    # incomplete.
                    plan = Plan(
                        subtasks=[
                            Subtask(
                                task_type="general",
                                prompt=request,
                                depends_on=[],
                            )
                        ],
                        complexity="normal",
                    )
                self.last_ask_cache_hit = False
                self.last_matched_skill = skill_match
                timings.plan_source = "skill"
                # 0.1.49 — close the data loop. Bump run_count and
                # last_run_at so /skill list shows which skills earn
                # their keep. Best-effort: a TOML write failure must
                # not break the ask, the skill still runs.
                try:
                    from anthill.core.recipes import save_recipe
                    skill_match.recipe.run_count += 1
                    skill_match.recipe.last_run_at = time.time()
                    save_recipe(skill_match.recipe, nation_dir)
                except Exception:  # noqa: BLE001
                    pass
            elif cached is not None:
                plan = cached.plan
                self.last_ask_cache_hit = True
                timings.plan_source = "cache"
            else:
                # v0.8.1 fast path — pre-Scout trivial classifier.
                # If the heuristic confidently labels this request as
                # trivial (single greeting, very short), skip Scout
                # entirely and build a one-subtask plan. Saves one LLM
                # round trip + tokens that would have gone into planning
                # a one-word answer.
                from anthill.core.complexity import fast_classify
                fast = fast_classify(request)
                if fast == "trivial":
                    plan = Plan(
                        subtasks=[
                            Subtask(
                                task_type="general",
                                prompt=request.strip(),
                                depends_on=[],
                            )
                        ],
                        complexity="trivial",
                    )
                    timings.plan_source = "trivial"
                else:
                    similar_block, episodic_sources = (
                        self._similar_past_block_with_sources(request)
                    )
                    workflow_block = self._workflow_templates_block()
                    plugin_block = self._plugin_stats_block()
                    # 0.1.15 — project context. When the REPL is in
                    # a git repo / pyproject / etc, Scout sees the
                    # project name + kind + top-level listing so its
                    # plan can reference real filenames.
                    # 0.1.33 — but only when the REQUEST actually
                    # references the local project. A general query
                    # like "find me an AI project to research" would
                    # otherwise get the local project's name fused
                    # into the answer (real-user-reported bug).
                    from anthill.core.project import (
                        find_project_root,
                        is_project_relevant_request,
                        project_context_block,
                    )
                    mode = self.project_inject_mode
                    if mode == "off":
                        project_block = ""
                    elif mode == "on":
                        project_block = project_context_block(
                            find_project_root()
                        )
                    else:  # "auto"
                        if is_project_relevant_request(request):
                            project_block = project_context_block(
                                find_project_root()
                            )
                        else:
                            project_block = ""
                    # 0.2.6 — inject anthill's self-knowledge when the
                    # user asks ABOUT anthill ("你能..." / "anthill 怎么用").
                    # Without this, models answer abstractly because
                    # they don't know they're inside anthill. Costs
                    # ~400 tokens per self-referential ask, zero
                    # otherwise.
                    self_block = ""
                    self_referential = False
                    try:
                        from anthill.core.self_context import (
                            looks_self_referential,
                            self_context_block,
                        )
                        self_referential = looks_self_referential(request)
                        if self_referential:
                            from anthill.core.userconfig import load_config
                            self_block = self_context_block(
                                load_config(), nation_name=self.name
                            )
                    except Exception:  # noqa: BLE001
                        self_block = ""
                        self_referential = False
                    if self_referential:
                        # 0.2.8 — self-referential ask = NEW topic about
                        # anthill itself. Past asks (probably mysql /
                        # zentao) and inferred workflow templates would
                        # confuse Scout into asking "are you talking
                        # about mysql or anthill?". Suppress everything
                        # except self-knowledge. The user wants a
                        # concrete answer about anthill, not a
                        # cross-topic deliberation.
                        episodic_context = self_block
                        # Mining sources are noise here too.
                        episodic_sources = []
                    else:
                        episodic_context = "\n\n".join(
                            b for b in (
                                self_block, project_block, workflow_block,
                                plugin_block, similar_block,
                            ) if b
                        )
                    scout = Scout(model=self.scout_model)
                    # 0.1.44 — capture Scout wall-clock so timing line
                    # can show "Scout: 3.1s" separately from subtasks.
                    _scout_t0 = time.perf_counter()
                    plan = await scout.plan(
                        request,
                        known_task_types=self.culture.known_task_types(),
                        episodic_context=episodic_context,
                        memory_context=self.memory_context,
                    )
                    timings.scout_seconds = time.perf_counter() - _scout_t0
                    timings.plan_source = "scout"
                    # If fast_classify was confident the request is
                    # complex (regex caught a 'research' / 'analyze'
                    # marker), honor that over whatever Scout claimed —
                    # the heuristic exists precisely because Scout
                    # sometimes underestimates depth on short complex
                    # prompts.
                    if fast == "complex" and plan.complexity != "complex":
                        plan.complexity = "complex"
                cache_remember(request, plan, self.plan_cache)
                self.last_ask_cache_hit = False
            # 0.1.13 — plan review hook. Fires only when Scout actually
            # ran (not on resume / pre_plan / cache hit / trivial fast).
            # The callback gets the Plan and returns either:
            #   - a (possibly modified) Plan to execute
            #   - None to cancel the ask entirely
            # For non-Scout paths we keep the existing behavior (plan
            # runs as-is) since those are either deterministic (recipe,
            # resume) or already optimized (cache, trivial).
            if (
                on_plan is not None
                and not self.last_ask_cache_hit
                and plan.complexity != "trivial"
            ):
                reviewed = await on_plan(plan)
                if reviewed is None:
                    return AskResult(
                        request=request,
                        plan=plan,
                        outcomes=[],
                        episodic_sources=episodic_sources,
                        cancelled_by_user=True,
                    )
                plan = reviewed
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
            initial_forbid=forbid,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
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

        # 0.1.44 — fold per-subtask wall-clock into timings. Each
        # outcome's started_at/ended_at is time.time() set by the
        # executor; the delta is the subtask's real time-on-the-clock
        # including retries. Refusal-retry count comes from scanning
        # attempts for failure_reason == "user_serving_refusal" (the
        # 0.1.40 marker). Total wall-clock is from ask() entry.
        timings.subtask_seconds = [
            (
                o.subtask.task_type,
                max(0.0, (o.ended_at or 0.0) - (o.started_at or 0.0)),
            )
            for o in outcomes
        ]
        timings.refusal_retry_count = sum(
            1
            for o in outcomes
            for a in o.attempts
            if getattr(a, "failure_reason", None) == "user_serving_refusal"
        )
        timings.total_seconds = time.perf_counter() - ask_started_perf

        return AskResult(
            request=request,
            plan=plan,
            outcomes=outcomes,
            ask_id=inflight.ask_id if inflight is not None else None,
            budget=snapshot(tracker) if tracker is not None else None,
            replans=replans_done,
            episodic_sources=episodic_sources,
            timings=timings,
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
        text, _ = self._similar_past_block_with_sources(request)
        return text

    def _similar_past_block_with_sources(
        self, request: str
    ) -> tuple[str, list[str]]:
        """Same as `_similar_past_block` but also returns the entry IDs.

        Used by Nation.ask to populate AskResult.episodic_sources so the
        REPL can render "📚 borrowed from these past asks." Keeping
        the simpler `_similar_past_block` as a thin wrapper preserves
        the API for any caller that just wanted the text.
        """
        if self.history_path is None or not self.history_path.exists():
            return "", []
        from anthill.core.history import load_history
        entries = load_history(self.history_path.parent, limit=200)
        if not entries:
            return "", []
        hits = find_similar(request, entries, top_k=3, min_score=0.2)
        sources = [h.entry.id for h in hits]
        return format_similar_for_scout(hits), sources

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
