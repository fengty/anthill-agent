"""Statecraft — execute a Plan as a DAG, with dependency-aware context passing.

This is what turns Anthill from "a thing that routes single tasks" into
"a thing that completes complex work." Before this module, a Plan with
three subtasks ran like three independent calls; the second never saw
the first's output. After this module, the second receives the first's
output as context, the third receives both, and the final synthesis
step has everything to draw on.

The mechanism is intentionally simple:

    1. Topological sort the subtasks by their declared depends_on.
    2. For each subtask in order, build a context block of every
       dependency's actual output, prepended to the prompt.
    3. Run via the nation's normal pheromone-routed pipeline.

The DAG is currently executed sequentially even when independent
subtasks could run in parallel. That's deliberate for v0.0.7 —
correctness and observability first, parallelism when the API budget
and debugging story can absorb it.

Dependency resolution is by task_type. When two subtasks share a
task_type, "depends on X" resolves to the most recent X executed
before this point. That's a simple rule, easy to reason about, and
matches how a human reviewer would read the plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from anthill.core.scout import Plan, Subtask

if TYPE_CHECKING:
    from anthill.core.agent import TaskResult
    from anthill.core.nation import Nation


@dataclass
class ExecutorError(Exception):
    """Raised when a Plan cannot be executed as written."""

    message: str

    def __str__(self) -> str:
        return self.message


def topological_order(plan: Plan) -> list[int]:
    """Return subtask indices in a valid execution order.

    Edges run from `depends_on` task_types to the subtasks that depend on
    them. A subtask depends on the most recent earlier subtask with a
    matching task_type. If a cited dependency does not appear in the plan
    at all, we surface that explicitly — silently ignoring it would let
    Scout drift toward referencing made-up types.
    """
    n = len(plan.subtasks)

    # Build the edge set: for each subtask, the indices of subtasks it
    # depends on. Walking depends_on in plan order means "latest matching
    # task_type before me" — exactly what a reader expects.
    deps: list[set[int]] = [set() for _ in range(n)]
    declared_types: set[str] = {st.task_type for st in plan.subtasks}

    for i, subtask in enumerate(plan.subtasks):
        for dep_type in subtask.depends_on:
            if dep_type not in declared_types:
                raise ExecutorError(
                    f"Subtask #{i} ({subtask.task_type}) depends on "
                    f"'{dep_type}', which no other subtask in this plan produces."
                )
            # Find the most recent prior subtask with this type.
            for j in range(i - 1, -1, -1):
                if plan.subtasks[j].task_type == dep_type:
                    deps[i].add(j)
                    break
            else:
                raise ExecutorError(
                    f"Subtask #{i} ({subtask.task_type}) depends on "
                    f"'{dep_type}', but no earlier subtask of that type exists."
                )

    # Kahn's algorithm — pull in nodes with no remaining incoming edges,
    # break ties by original plan order so output is deterministic.
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
    """Format the outputs this subtask depends on as a context block.

    Empty string if the subtask has no dependencies — the worker should
    not see a stray 'Previous results:' header for no reason.
    """
    if not subtask.depends_on:
        return ""

    sections: list[str] = []
    for dep_type in subtask.depends_on:
        result = completed.get(dep_type)
        if result is None:
            continue  # topological_order already validated this; defensive only
        sections.append(f"[{dep_type}]\n{result.output}")

    if not sections:
        return ""

    return "Previous results:\n\n" + "\n\n".join(sections) + "\n\n---\n\n"


async def execute_plan(plan: Plan, nation: "Nation") -> list["TaskResult"]:
    """Run a Plan with dependency-aware context passing.

    Returns results in plan order (not topological order) so callers can
    align them with the user-facing plan display.
    """
    order = topological_order(plan)
    results_by_index: dict[int, "TaskResult"] = {}
    latest_by_type: dict[str, "TaskResult"] = {}

    for i in order:
        subtask = plan.subtasks[i]
        context = build_context_block(subtask, latest_by_type)
        augmented_prompt = context + subtask.prompt if context else subtask.prompt
        result = await nation.run(subtask.task_type, augmented_prompt)
        results_by_index[i] = result
        latest_by_type[subtask.task_type] = result

    return [results_by_index[i] for i in range(len(plan.subtasks))]
