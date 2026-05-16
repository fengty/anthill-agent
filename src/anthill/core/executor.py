"""Statecraft — execute a Plan as a DAG, with retries and graceful failure.

Three problems this module solves at once:

1. **Dependency-aware context passing.** When subtask B depends on A, B's
   prompt is prepended with A's output. Without this, multi-step plans
   are just three independent calls.

2. **Retries with citizen rotation.** A transient API failure should not
   kill the whole request. When a subtask fails, the executor tries the
   same subtask on a *different* citizen, up to `max_attempts` times. The
   router's `forbid` parameter is what makes "different citizen" possible.

3. **Fail-fast on broken dependencies.** If `research` fails after all
   retries, there is no point running `compare` and `recommend` — they
   would receive garbage as context and produce garbage. The executor
   marks downstream subtasks as `skipped` instead.

A `SubtaskOutcome` carries the whole trace per subtask: every attempt
(success or failure), the final status, and the result the user-facing
output should draw on.

The DAG is currently executed sequentially even when independent
subtasks could run in parallel. That's deliberate for v0.0.8 —
correctness, retries, and observability first; parallelism in v0.0.9.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

from anthill.core.scout import Plan, Subtask

if TYPE_CHECKING:
    from anthill.core.agent import TaskResult
    from anthill.core.budget import BudgetTracker
    from anthill.core.nation import Nation


SubtaskStatus = Literal["ok", "failed", "skipped"]


@dataclass
class ExecutorError(Exception):
    """Raised when a Plan cannot be executed as written (structural error)."""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass
class RetryPolicy:
    """How hard the executor tries before giving up on a subtask.

    max_attempts counts the total number of tries including the first one.
    A value of 1 means no retries; a value of 3 means one original + two
    retries. Retries always pick a *different* citizen from the one that
    just failed.
    """

    max_attempts: int = 3
    skip_downstream_on_failure: bool = True
    parallel: bool = True  # run independent subtasks via asyncio.gather


@dataclass
class SubtaskOutcome:
    """Everything we know about how one subtask in a plan went."""

    subtask: Subtask
    attempts: list["TaskResult"] = field(default_factory=list)
    status: SubtaskStatus = "ok"
    skip_reason: str | None = None  # filled when status == "skipped"
    started_at: float | None = None
    ended_at: float | None = None

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None or self.ended_at is None:
            return 0.0
        return self.ended_at - self.started_at

    @property
    def final(self) -> "TaskResult | None":
        if not self.attempts:
            return None
        return self.attempts[-1]

    @property
    def output(self) -> str:
        if self.status == "skipped":
            return f"[skipped: {self.skip_reason}]"
        last = self.final
        return str(last.output) if last is not None else ""


# Progress event types — what callers learn about while a plan runs.
@dataclass
class ProgressEvent:
    """A single observable event during execute_plan.

    kind:
      'started'    a subtask just started its first attempt
      'attempt'    a subtask attempt completed (success or failure)
      'finished'   a subtask reached its final status (ok/failed/skipped)
    """

    kind: Literal["started", "attempt", "finished"]
    index: int           # subtask index in plan order
    subtask: Subtask
    outcome: SubtaskOutcome
    attempt_number: int = 0   # 1-based; 0 for 'started' / 'finished'
    success: bool = False


# Type alias for the async callback consumers register.
ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


def topological_order(plan: Plan) -> list[int]:
    """Return subtask indices in a valid execution order.

    A subtask depends on the most recent earlier subtask with a matching
    task_type. Forward references (depending on something later) and
    dangling references (depending on something that doesn't exist) both
    raise — silently dropping them would let Scout's plans drift toward
    referencing imagined types.
    """
    n = len(plan.subtasks)
    deps: list[set[int]] = [set() for _ in range(n)]
    declared_types: set[str] = {st.task_type for st in plan.subtasks}

    for i, subtask in enumerate(plan.subtasks):
        for dep_type in subtask.depends_on:
            if dep_type not in declared_types:
                raise ExecutorError(
                    f"Subtask #{i} ({subtask.task_type}) depends on "
                    f"'{dep_type}', which no other subtask in this plan produces."
                )
            for j in range(i - 1, -1, -1):
                if plan.subtasks[j].task_type == dep_type:
                    deps[i].add(j)
                    break
            else:
                raise ExecutorError(
                    f"Subtask #{i} ({subtask.task_type}) depends on "
                    f"'{dep_type}', but no earlier subtask of that type exists."
                )

    indegree = [len(d) for d in deps]
    ready = [i for i in range(n) if indegree[i] == 0]
    ordered: list[int] = []

    while ready:
        ready.sort()
        i = ready.pop(0)
        ordered.append(i)
        for j in range(n):
            if i in deps[j]:
                deps[j].remove(i)
                indegree[j] -= 1
                if indegree[j] == 0:
                    ready.append(j)

    if len(ordered) != n:
        raise ExecutorError("Plan contains a dependency cycle.")
    return ordered


def build_context_block(
    subtask: Subtask,
    completed: dict[str, "TaskResult"],
) -> str:
    """Format dependency outputs as a context block prepended to the prompt."""
    if not subtask.depends_on:
        return ""

    sections: list[str] = []
    for dep_type in subtask.depends_on:
        result = completed.get(dep_type)
        if result is None:
            continue
        sections.append(f"[{dep_type}]\n{result.output}")

    if not sections:
        return ""
    return "Previous results:\n\n" + "\n\n".join(sections) + "\n\n---\n\n"


def _was_successful(result: "TaskResult") -> bool:
    """A simple, deterministic success check.

    The agent layer already collapses both exceptions and empty responses
    to score=0.0. Using the score keeps the executor independent of the
    failure mode.
    """
    return result.success_score > 0.0


async def _run_one_subtask(
    i: int,
    subtask: Subtask,
    plan: Plan,
    nation: "Nation",
    outcomes: dict[int, SubtaskOutcome],
    latest_by_type: dict[str, "TaskResult"],
    policy: RetryPolicy,
    on_progress: ProgressCallback | None = None,
    budget: "BudgetTracker | None" = None,
) -> None:
    """Execute one subtask with retries; mutate outcomes in place.

    Emits ProgressEvents at three points: started, after each attempt,
    and once final status is known. Callers (REPL, CLI, web) subscribe
    to these to render progress live.

    When `budget` is provided, the tracker is consulted before any
    work runs and after each attempt. An exhausted budget converts the
    subtask into a 'skipped' outcome with a reason that names which
    cap blew (tokens/cost/time).
    """
    outcome = outcomes[i]
    failed_dep = _find_failed_dependency(subtask, plan, outcomes)
    if failed_dep is not None and policy.skip_downstream_on_failure:
        outcome.status = "skipped"
        outcome.skip_reason = f"dependency '{failed_dep}' failed"
        outcome.started_at = outcome.ended_at = time.time()
        if on_progress is not None:
            await on_progress(
                ProgressEvent(
                    kind="finished",
                    index=i,
                    subtask=subtask,
                    outcome=outcome,
                )
            )
        return

    # Pre-flight budget check — never start a subtask we can't afford.
    if budget is not None:
        from anthill.core.budget import reason_label
        why = budget.may_run_next()
        if why is not None:
            outcome.status = "skipped"
            outcome.skip_reason = reason_label(why)
            outcome.started_at = outcome.ended_at = time.time()
            if on_progress is not None:
                await on_progress(
                    ProgressEvent(
                        kind="finished",
                        index=i,
                        subtask=subtask,
                        outcome=outcome,
                    )
                )
            return

    outcome.started_at = time.time()
    if on_progress is not None:
        await on_progress(
            ProgressEvent(
                kind="started",
                index=i,
                subtask=subtask,
                outcome=outcome,
            )
        )

    context = build_context_block(subtask, latest_by_type)
    augmented_prompt = context + subtask.prompt if context else subtask.prompt

    succeeded = False
    fanout = max(1, getattr(subtask, "fanout", 1))

    if fanout > 1:
        # v0.6 ensemble path — parallel K-way fan-out + strategy pick.
        from anthill.core.ensemble import run_fanout, select_winner
        attempts_in_wave = await run_fanout(
            nation, subtask, augmented_prompt, fanout
        )
        # All attempts get recorded — the selector picks one to be the
        # "winner" whose output feeds downstream; the others stay in
        # outcome.attempts for transparency.
        outcome.attempts.extend(attempts_in_wave)
        if budget is not None:
            for r in attempts_in_wave:
                budget.record_attempt(r.agent_id, r.input_tokens, r.output_tokens)
        if on_progress is not None:
            for attempt_idx, r in enumerate(attempts_in_wave, start=1):
                await on_progress(
                    ProgressEvent(
                        kind="attempt",
                        index=i,
                        subtask=subtask,
                        outcome=outcome,
                        attempt_number=attempt_idx,
                        success=_was_successful(r),
                    )
                )
        if attempts_in_wave:
            winner = select_winner(
                attempts_in_wave,
                strategy=getattr(subtask, "strategy", "first_success"),
            )
            if _was_successful(winner):
                latest_by_type[subtask.task_type] = winner
                succeeded = True
    else:
        # Legacy serial retry path — unchanged behavior for fanout=1.
        forbid: set[str] = set()
        for attempt_idx in range(policy.max_attempts):
            try:
                result = await nation.run(subtask.task_type, augmented_prompt, forbid=forbid)
            except RuntimeError:
                break
            outcome.attempts.append(result)
            if budget is not None:
                budget.record_attempt(
                    result.agent_id, result.input_tokens, result.output_tokens
                )
            attempt_ok = _was_successful(result)
            if on_progress is not None:
                await on_progress(
                    ProgressEvent(
                        kind="attempt",
                        index=i,
                        subtask=subtask,
                        outcome=outcome,
                        attempt_number=attempt_idx + 1,
                        success=attempt_ok,
                    )
                )
            if attempt_ok:
                latest_by_type[subtask.task_type] = result
                succeeded = True
                break
            forbid.add(result.agent_id)
            # Don't burn another retry attempt past the budget cap.
            if budget is not None and budget.may_run_next() is not None:
                break

    outcome.status = "ok" if succeeded else "failed"
    outcome.ended_at = time.time()
    if on_progress is not None:
        await on_progress(
            ProgressEvent(
                kind="finished",
                index=i,
                subtask=subtask,
                outcome=outcome,
            )
        )


def _waves_from_topological_order(plan: Plan, order: list[int]) -> list[list[int]]:
    """Group subtasks into waves where every member of a wave is independent
    of every other member, and depends only on members of earlier waves.

    This is the natural shape of a DAG executed level-by-level. The first
    wave is all subtasks with no dependencies. The second wave is everything
    whose dependencies are all in wave 1. And so on.
    """
    depth_of: dict[int, int] = {}
    for i in order:
        deps_depth = -1
        for dep_type in plan.subtasks[i].depends_on:
            # Find the latest matching earlier subtask of this dep_type.
            for j in range(i - 1, -1, -1):
                if plan.subtasks[j].task_type == dep_type:
                    deps_depth = max(deps_depth, depth_of.get(j, 0))
                    break
        depth_of[i] = deps_depth + 1

    waves: dict[int, list[int]] = {}
    for i, d in depth_of.items():
        waves.setdefault(d, []).append(i)
    return [sorted(waves[d]) for d in sorted(waves)]


async def execute_plan(
    plan: Plan,
    nation: "Nation",
    *,
    retry: RetryPolicy | None = None,
    on_progress: ProgressCallback | None = None,
    resume_state: dict[int, SubtaskOutcome] | None = None,
    budget: "BudgetTracker | None" = None,
) -> list[SubtaskOutcome]:
    """Run a Plan with retries, citizen rotation, and graceful skipping.

    When `retry.parallel` is true, independent subtasks within the same
    DAG wave run via asyncio.gather. Subtasks across waves stay strictly
    ordered — a downstream wave cannot start until its dependencies are
    done. This matches the semantics of the sequential path exactly;
    only the wall-clock cost of independent branches changes.

    If `on_progress` is provided, it receives ProgressEvent objects as
    each subtask starts, retries, and finishes. The callback is async
    so callers can do I/O (print to terminal, push to a queue) without
    blocking the executor.

    If `resume_state` is provided, each pre-completed outcome it carries
    is treated as already-done: the subtask is not re-run, its output is
    made available as context for downstream subtasks, and a single
    'finished' ProgressEvent is emitted so UI consumers can render it.
    Only outcomes with status='ok' are honored; the executor never
    pre-loads failed/skipped state because resume's whole purpose is to
    retry what didn't finish.

    Returns one SubtaskOutcome per subtask, in plan order.
    """
    policy = retry or RetryPolicy()
    order = topological_order(plan)
    outcomes: dict[int, SubtaskOutcome] = {
        i: SubtaskOutcome(subtask=plan.subtasks[i]) for i in range(len(plan.subtasks))
    }
    latest_by_type: dict[str, "TaskResult"] = {}

    resumed_indices: set[int] = set()
    if resume_state:
        for i, pre in resume_state.items():
            if pre.status != "ok" or pre.final is None:
                # Defensive: refuse to honor anything that isn't a clean win.
                continue
            outcomes[i] = pre
            latest_by_type[plan.subtasks[i].task_type] = pre.final
            resumed_indices.add(i)

    # Surface the resumed steps as 'finished' events up front so consumers
    # render them as done before we start working on the new waves.
    if on_progress is not None:
        for i in sorted(resumed_indices):
            await on_progress(
                ProgressEvent(
                    kind="finished",
                    index=i,
                    subtask=plan.subtasks[i],
                    outcome=outcomes[i],
                )
            )

    if policy.parallel:
        waves = _waves_from_topological_order(plan, order)
        for wave in waves:
            pending = [i for i in wave if i not in resumed_indices]
            if not pending:
                continue
            await asyncio.gather(
                *(
                    _run_one_subtask(
                        i,
                        plan.subtasks[i],
                        plan,
                        nation,
                        outcomes,
                        latest_by_type,
                        policy,
                        on_progress,
                        budget,
                    )
                    for i in pending
                )
            )
    else:
        for i in order:
            if i in resumed_indices:
                continue
            await _run_one_subtask(
                i,
                plan.subtasks[i],
                plan,
                nation,
                outcomes,
                latest_by_type,
                policy,
                on_progress,
                budget,
            )

    return [outcomes[i] for i in range(len(plan.subtasks))]


def _find_failed_dependency(
    subtask: Subtask,
    plan: Plan,
    outcomes: dict[int, SubtaskOutcome],
) -> str | None:
    """Return the task_type of the first failed/skipped dependency, or None."""
    for dep_type in subtask.depends_on:
        # Same resolution rule as topological_order: latest matching earlier.
        for j in range(len(plan.subtasks) - 1, -1, -1):
            if plan.subtasks[j].task_type == dep_type:
                if outcomes[j].status != "ok":
                    return dep_type
                break
    return None
