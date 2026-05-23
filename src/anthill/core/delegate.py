"""0.2.32 — delegate_task tool: citizens dispatch sub-tasks to peers.

The shape: citizen A is running an agent loop on a complex ask.
Mid-loop it realizes "this sub-piece needs the research specialist
not me." It emits a `delegate_task` tool call. Anthill spawns a
fresh agent_loop on a different citizen (router-selected for the
given task_type, parent forbidden), runs it to completion, returns
the final text back to citizen A as the tool result.

This is the foundation of multi-agent collaboration. Combined
with kanban (0.2.31), agents can both create persistent work for
each other (kanban_create) AND synchronously delegate sub-tasks
(delegate_task).

Safety:
  - Recursion depth capped (default 3). A planner → specialist →
    specialist-2 → specialist-3 chain is the deepest case we
    expect. Beyond that smells like a model stuck in a loop.
  - Parent's agent_id is added to the child's forbid set so we
    don't trivially route back to self.
  - The child gets its own _delegation_depth via the nation
    counter so its own delegate calls respect the cap.
"""

from __future__ import annotations

from anthill.core.tools_protocol import ToolCall, ToolResult, ToolSpec


# Process-wide cap on chained delegation. A bigger value isn't
# inherently dangerous (each level is bounded by per-loop
# max_iterations) but it usually means the model is misusing the
# tool. Real-world flows we've seen need ≤2 levels.
MAX_DELEGATION_DEPTH: int = 3


DELEGATE_TASK = ToolSpec(
    name="delegate_task",
    description=(
        "Dispatch a self-contained sub-task to a specialist citizen "
        "for the given task_type. The router picks the best-fit "
        "model (by pheromone), runs its own agent loop to "
        "completion, returns the final answer back to you. Use this "
        "when a sub-component would benefit from a different model. "
        "Don't delegate things you can do trivially yourself — "
        "delegation adds latency. Recursion is capped at depth 3."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "description": (
                    "Categorical label (e.g. 'research', 'analyze', "
                    "'summarize', 'implement'). Pheromone trails "
                    "for this label decide which citizen is picked."
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "What the specialist should do. Self-contained — "
                    "they don't see your conversation history."
                ),
            },
        },
        "required": ["task_type", "prompt"],
    },
)


def make_delegate_executor(nation, parent_agent_id: str):
    """Create the executor closure that knows the calling citizen.

    `nation` is the Nation instance (we'll call .run on it).
    `parent_agent_id` is who's invoking — used to forbid self-routing.

    Returns an async function matching the agent_loop executor sig.
    """

    async def execute(call: ToolCall) -> ToolResult:
        # Depth gate first — cheap, prevents the recursion fan-out
        # before we do any router work.
        depth = getattr(nation, "_delegation_depth", 0)
        if depth >= MAX_DELEGATION_DEPTH:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"delegation depth limit reached "
                    f"(max {MAX_DELEGATION_DEPTH}). Do the work "
                    f"yourself or kanban_create a follow-up task."
                ),
                is_error=True,
            )
        args = call.arguments or {}
        task_type = args.get("task_type")
        prompt = args.get("prompt")
        if not task_type or not isinstance(task_type, str):
            return ToolResult(
                tool_call_id=call.id,
                content="delegate_task requires `task_type` string",
                is_error=True,
            )
        if not prompt or not isinstance(prompt, str):
            return ToolResult(
                tool_call_id=call.id,
                content="delegate_task requires `prompt` string",
                is_error=True,
            )

        # Forbid the parent so the router picks a peer. Without
        # this, a planner citizen could trivially re-route work
        # back to itself, which is just doing the work in-line
        # with extra hops.
        forbid = {parent_agent_id} if parent_agent_id else None

        # Increment / decrement the depth counter so the child's
        # own delegate calls see the right level.
        nation._delegation_depth = depth + 1  # type: ignore[attr-defined]
        try:
            result = await nation.run(task_type, prompt, forbid=forbid)
        except Exception as e:  # noqa: BLE001 — convert to tool error
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"delegate crashed: {type(e).__name__}: {e}"
                ),
                is_error=True,
            )
        finally:
            nation._delegation_depth = depth  # type: ignore[attr-defined]

        # Surface the child's identity so the parent's narrative can
        # cite who did the work — useful in audit / debugging.
        attribution = f"# delegated to {result.agent_id} (task_type={task_type})"
        is_error = result.success_score < 0.5
        body = result.output or "(no output)"
        return ToolResult(
            tool_call_id=call.id,
            content=f"{attribution}\n\n{body}",
            is_error=is_error,
        )

    return execute
