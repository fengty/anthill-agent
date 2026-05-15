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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from anthill.core.scout import Plan, Subtask

if TYPE_CHECKING:
    from anthill.core.agent import TaskResult
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


@dataclass
class SubtaskOutcome:
    """Everything we know about how one subtask in a plan went."""

    subtask: Subtask
    attempts: list["TaskResult"] = field(default_factory=list)
    status: SubtaskStatus = "ok"
    skip_reason: str | None = None  # filled when status == "skipped"

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


async def execute_plan(
    plan: Plan,
    nation: "Nation",
    *,
    retry: RetryPolicy | None = None,
) -> list[SubtaskOutcome]:
    """Run a Plan with retries, citizen rotation, and graceful skipping.

    Returns one SubtaskOutcome per subtask, in plan order.
    """
    policy = retry or RetryPolicy()
    order = topological_order(plan)
    outcomes: dict[int, SubtaskOutcome] = {
        i: SubtaskOutcome(subtask=plan.subtasks[i]) for i in range(len(plan.subtasks))
    }
    latest_by_type: dict[str, "TaskResult"] = {}

    for i in order:
        subtask = plan.subtasks[i]

        # Fail-fast: if any dependency was not successful, skip this subtask.
        failed_dep = _find_failed_dependency(subtask, plan, outcomes)
        if failed_dep is not None and policy.skip_downstream_on_failure:
            outcomes[i].status = "skipped"
            outcomes[i].skip_reason = f"dependency '{failed_dep}' failed"
            continue

        context = build_context_block(subtask, latest_by_type)
        augmented_prompt = context + subtask.prompt if context else subtask.prompt

        forbid: set[str] = set()
        succeeded = False
        for _attempt in range(policy.max_attempts):
            try:
                result = await nation.run(subtask.task_type, augmented_prompt, forbid=forbid)
            except RuntimeError:
                # No eligible citizen left — every candidate has already
                # failed this attempt. Stop retrying.
                break
            outcomes[i].attempts.append(result)
            if _was_successful(result):
                latest_by_type[subtask.task_type] = result
                succeeded = True
                break
            forbid.add(result.agent_id)

        outcomes[i].status = "ok" if succeeded else "failed"

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
