"""0.2.31 — Kanban tools exposed to the agent loop.

Citizens running in agentic_mode see these tools alongside
bash_run / browser_action. They can:

  - kanban_show       — read the active board / one task
  - kanban_create     — file a task for later / another citizen
  - kanban_complete   — close out a task with a handoff summary
  - kanban_block      — stop with "needs human" / "needs other task"
  - kanban_comment    — leave breadcrumbs on a task
  - kanban_claim      — atomically claim the next pending task

The schemas are intentionally lean — every field has to justify
itself by being something a downstream reader (next citizen or
the user) will actually look at.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from anthill.core.kanban import (
    KanbanTask,
    add_comment,
    board_summary,
    claim_next,
    create_task,
    list_comments,
    list_tasks,
    show_task,
    update_status,
)
from anthill.core.tools_protocol import ToolCall, ToolResult, ToolSpec


# --- specs ------------------------------------------------------------


KANBAN_SHOW = ToolSpec(
    name="kanban_show",
    description=(
        "Show one task in detail (with comments) or the active board "
        "(non-completed tasks) when no id is given. Use this to read "
        "what's pending before deciding to work."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "Optional task id."},
            "limit": {
                "type": "integer",
                "description": "When listing, max tasks to return. Default 20.",
            },
        },
    },
)


KANBAN_CREATE = ToolSpec(
    name="kanban_create",
    description=(
        "File a new task on the board. Use this when work surfaces "
        "that should be tracked but doesn't fit the current ask, or "
        "to hand off to another citizen later."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short imperative title (5-12 words).",
            },
            "body": {
                "type": "string",
                "description": "Optional details / acceptance criteria.",
            },
            "parent_id": {
                "type": "integer",
                "description": (
                    "Optional parent task id. Use when this task "
                    "is a sub-task of one you're working on."
                ),
            },
        },
        "required": ["title"],
    },
)


KANBAN_COMPLETE = ToolSpec(
    name="kanban_complete",
    description=(
        "Mark a task done with a 1-3 sentence summary of what was "
        "actually done and any artifacts (file paths, commit SHAs). "
        "Downstream tasks read this — keep it concrete."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "summary": {
                "type": "string",
                "description": "1-3 sentences. Name concrete artifacts.",
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Optional machine-readable handoff "
                    "(changed_files / tests_run / etc.)."
                ),
            },
        },
        "required": ["id", "summary"],
    },
)


KANBAN_BLOCK = ToolSpec(
    name="kanban_block",
    description=(
        "Mark a task blocked because you need a human decision you "
        "can't infer (missing credentials, UX choice, peer output). "
        "Don't guess; stop and wait."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "reason": {
                "type": "string",
                "description": "Why you can't proceed. Specific.",
            },
        },
        "required": ["id", "reason"],
    },
)


KANBAN_COMMENT = ToolSpec(
    name="kanban_comment",
    description=(
        "Add a comment on a task. Use for progress notes, partial "
        "results, or anything the next reader of this task should "
        "see."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "text": {"type": "string"},
        },
        "required": ["id", "text"],
    },
)


KANBAN_CLAIM = ToolSpec(
    name="kanban_claim",
    description=(
        "Atomically claim the oldest unassigned pending task as "
        "yourself. Returns the task or 'no pending tasks' if the "
        "board is empty."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": (
                    "Your identifier. Usually an agent_id. "
                    "Used so concurrent claimers don't double-pull."
                ),
            },
        },
        "required": ["assignee"],
    },
)


KANBAN_TOOLS: tuple[ToolSpec, ...] = (
    KANBAN_SHOW, KANBAN_CREATE, KANBAN_COMPLETE,
    KANBAN_BLOCK, KANBAN_COMMENT, KANBAN_CLAIM,
)


# --- formatter --------------------------------------------------------


def _format_task(task: KanbanTask, comments: list = None) -> str:
    """Render one task as the model will see it."""
    lines = [
        f"#{task.id} [{task.status}] {task.title}",
    ]
    if task.assignee:
        lines.append(f"assignee: {task.assignee}")
    if task.parent_id is not None:
        lines.append(f"parent: #{task.parent_id}")
    if task.body:
        lines.append("")
        lines.append(task.body)
    if task.summary:
        lines.append("")
        lines.append(f"summary: {task.summary}")
    if comments:
        lines.append("")
        lines.append("--- comments ---")
        for c in comments:
            who = c.author or "user"
            lines.append(f"[{who}] {c.text}")
    return "\n".join(lines)


def _format_task_list(tasks: list) -> str:
    if not tasks:
        return "(no active tasks)"
    lines = []
    for t in tasks:
        marker = {
            "pending": "·",
            "in_progress": "▶",
            "blocked": "✋",
            "completed": "✓",
            "cancelled": "✗",
        }.get(t.status, "?")
        suffix = f"  ({t.assignee})" if t.assignee else ""
        lines.append(f"  {marker} #{t.id} {t.title}{suffix}")
    summary = board_summary_text(tasks)
    if summary:
        lines.append("")
        lines.append(summary)
    return "\n".join(lines)


def board_summary_text(tasks: list) -> str:
    """A one-line summary for status banners."""
    if not tasks:
        return ""
    by_status: dict[str, int] = {}
    for t in tasks:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    parts = [f"{n} {s}" for s, n in sorted(by_status.items()) if n]
    return "(" + ", ".join(parts) + ")"


# --- executors --------------------------------------------------------


def make_kanban_executors(home: Path, default_assignee: Optional[str] = None):
    """Bind kanban tools to a specific anthill home dir.

    Returns a dispatch function — pass it (or merge with the bash
    dispatch) to run_agent_loop's `executor` arg.

    `default_assignee` is the citizen id calling the tools; used
    when a tool needs an "actor" identity but the model omitted it
    (e.g. `kanban_claim` without `assignee`).
    """

    async def kanban_show(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        tid = args.get("id")
        if tid is not None:
            try:
                tid = int(tid)
            except (TypeError, ValueError):
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"id must be an integer, got {tid!r}",
                    is_error=True,
                )
            task = show_task(home, tid)
            if task is None:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"no task with id {tid}",
                    is_error=True,
                )
            comments = list_comments(home, tid)
            return ToolResult(
                tool_call_id=call.id,
                content=_format_task(task, comments),
            )
        # No id → list active board.
        limit = int(args.get("limit", 20))
        tasks = list_tasks(home, limit=limit)
        return ToolResult(
            tool_call_id=call.id,
            content=_format_task_list(tasks),
        )

    async def kanban_create_tool(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        title = args.get("title")
        if not title or not isinstance(title, str):
            return ToolResult(
                tool_call_id=call.id,
                content="kanban_create requires `title` string",
                is_error=True,
            )
        body = args.get("body") or ""
        parent = args.get("parent_id")
        if parent is not None:
            try:
                parent = int(parent)
            except (TypeError, ValueError):
                parent = None
        try:
            tid = create_task(
                home,
                title=title,
                body=body,
                parent_id=parent,
                assignee=default_assignee,
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"create failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"created task #{tid}: {title}",
        )

    async def kanban_complete_tool(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        tid = args.get("id")
        summary = args.get("summary")
        if tid is None or not summary:
            return ToolResult(
                tool_call_id=call.id,
                content="kanban_complete requires `id` and `summary`",
                is_error=True,
            )
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return ToolResult(
                tool_call_id=call.id,
                content=f"id must be an integer, got {tid!r}",
                is_error=True,
            )
        metadata = args.get("metadata") or None
        ok = update_status(
            home, tid, "completed", summary=summary, metadata=metadata,
        )
        if not ok:
            return ToolResult(
                tool_call_id=call.id,
                content=f"no task with id {tid}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"task #{tid} completed",
        )

    async def kanban_block_tool(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        tid = args.get("id")
        reason = args.get("reason")
        if tid is None or not reason:
            return ToolResult(
                tool_call_id=call.id,
                content="kanban_block requires `id` and `reason`",
                is_error=True,
            )
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return ToolResult(
                tool_call_id=call.id,
                content=f"id must be an integer, got {tid!r}",
                is_error=True,
            )
        ok = update_status(home, tid, "blocked", summary=f"BLOCKED: {reason}")
        if not ok:
            return ToolResult(
                tool_call_id=call.id,
                content=f"no task with id {tid}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"task #{tid} blocked: {reason}",
        )

    async def kanban_comment_tool(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        tid = args.get("id")
        text = args.get("text")
        if tid is None or not text:
            return ToolResult(
                tool_call_id=call.id,
                content="kanban_comment requires `id` and `text`",
                is_error=True,
            )
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return ToolResult(
                tool_call_id=call.id,
                content=f"id must be an integer, got {tid!r}",
                is_error=True,
            )
        try:
            cid = add_comment(home, tid, text, author=default_assignee)
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"comment failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"comment #{cid} added to task #{tid}",
        )

    async def kanban_claim_tool(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        assignee = args.get("assignee") or default_assignee
        if not assignee:
            return ToolResult(
                tool_call_id=call.id,
                content="kanban_claim requires `assignee`",
                is_error=True,
            )
        task = claim_next(home, assignee)
        if task is None:
            return ToolResult(
                tool_call_id=call.id,
                content="no pending tasks",
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"claimed task #{task.id}: {task.title}\n\n{task.body}",
        )

    handlers = {
        "kanban_show": kanban_show,
        "kanban_create": kanban_create_tool,
        "kanban_complete": kanban_complete_tool,
        "kanban_block": kanban_block_tool,
        "kanban_comment": kanban_comment_tool,
        "kanban_claim": kanban_claim_tool,
    }

    async def dispatch(call: ToolCall) -> ToolResult:
        handler = handlers.get(call.name)
        if handler is None:
            return ToolResult(
                tool_call_id=call.id,
                content=f"unknown kanban tool: {call.name!r}",
                is_error=True,
            )
        return await handler(call)

    return dispatch, handlers
