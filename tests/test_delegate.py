"""0.2.32 — delegate_task: citizens dispatch sub-tasks to peers.

The contract: citizen A in its loop emits `delegate_task({
task_type: 'research', prompt: '...'})`. Anthill spawns a fresh
agent execution on the best-fit citizen (router-selected, parent
forbidden). The child's output becomes the tool result back to A.

Tests cover the executor in isolation — we mock Nation.run so we
don't need a real LLM. The router/pheromone/forbid plumbing is
already tested elsewhere; here we just verify delegate routes
correctly and respects depth + safety guards.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest

from anthill.core.agent import Agent, TaskResult
from anthill.core.delegate import (
    DELEGATE_TASK,
    MAX_DELEGATION_DEPTH,
    make_delegate_executor,
)
from anthill.core.nation import Nation
from anthill.core.tools_protocol import ToolCall, ToolResult


class _RecordingNation:
    """Stand-in for Nation that records what nation.run was called with
    and returns canned TaskResults. Lets us verify delegate semantics
    without spinning up the full router/provider stack."""

    def __init__(self, canned_outputs: dict[str, str] | None = None):
        self.calls: list[tuple[str, str, set | None]] = []
        self.canned = canned_outputs or {}
        # Mirror real Nation: depth counter lives here.
        self._delegation_depth = 0

    async def run(self, task_type, prompt, *, forbid=None, **kwargs):
        self.calls.append((task_type, prompt, forbid))
        output = self.canned.get(task_type, f"[stub for {task_type}]")
        return TaskResult(
            task_id=f"task-{uuid.uuid4().hex[:6]}",
            agent_id="ant-child",
            task_type=task_type,
            output=output,
            success_score=1.0,
            duration_seconds=0.05,
            input_tokens=10,
            output_tokens=20,
        )


# --- happy path ----------------------------------------------------


def test_delegate_routes_to_nation_run() -> None:
    """The executor invokes nation.run with the given task_type/prompt
    and forbids the parent agent."""
    n = _RecordingNation(canned_outputs={"research": "I found X"})
    exec_fn = make_delegate_executor(n, parent_agent_id="ant-parent")

    result = asyncio.run(exec_fn(ToolCall(
        id="d1", name="delegate_task",
        arguments={"task_type": "research", "prompt": "research X"},
    )))

    assert not result.is_error
    assert "I found X" in result.content
    # Parent forbidden so router picks a peer.
    assert n.calls == [("research", "research X", {"ant-parent"})]


def test_delegate_includes_attribution() -> None:
    """The tool result mentions which citizen did the work, useful
    for the parent's narrative + audit."""
    n = _RecordingNation()
    exec_fn = make_delegate_executor(n, parent_agent_id="ant-parent")
    result = asyncio.run(exec_fn(ToolCall(
        id="d1", name="delegate_task",
        arguments={"task_type": "analyze", "prompt": "x"},
    )))
    assert "ant-child" in result.content
    assert "task_type=analyze" in result.content


# --- depth cap ------------------------------------------------------


def test_depth_cap_blocks_chain_runaway() -> None:
    """When nation._delegation_depth is already at the max, the
    next delegate refuses."""
    n = _RecordingNation()
    n._delegation_depth = MAX_DELEGATION_DEPTH
    exec_fn = make_delegate_executor(n, parent_agent_id="ant-parent")

    result = asyncio.run(exec_fn(ToolCall(
        id="d1", name="delegate_task",
        arguments={"task_type": "x", "prompt": "y"},
    )))
    assert result.is_error
    assert "depth" in result.content.lower()
    # And nation.run was NOT called.
    assert n.calls == []


def test_depth_is_incremented_during_call() -> None:
    """Inside the delegated nation.run, the depth counter is bumped
    so the child's own delegate calls see the higher level. After
    the call returns, depth is restored."""
    n = _RecordingNation()
    saw_depth = []

    # Override run to peek at depth mid-call.
    orig_run = n.run

    async def peeking_run(task_type, prompt, *, forbid=None, **kwargs):
        saw_depth.append(n._delegation_depth)
        return await orig_run(task_type, prompt, forbid=forbid, **kwargs)

    n.run = peeking_run  # type: ignore[assignment]
    exec_fn = make_delegate_executor(n, parent_agent_id="ant-parent")
    asyncio.run(exec_fn(ToolCall(
        id="d1", name="delegate_task",
        arguments={"task_type": "x", "prompt": "y"},
    )))
    # During: depth was 1.
    assert saw_depth == [1]
    # After: depth restored to 0.
    assert n._delegation_depth == 0


def test_depth_restored_even_on_exception() -> None:
    """If nation.run raises, the finally block must restore depth."""
    n = _RecordingNation()

    async def crashing_run(*args, **kwargs):
        raise RuntimeError("boom")

    n.run = crashing_run  # type: ignore[assignment]
    exec_fn = make_delegate_executor(n, parent_agent_id="ant-parent")
    result = asyncio.run(exec_fn(ToolCall(
        id="d1", name="delegate_task",
        arguments={"task_type": "x", "prompt": "y"},
    )))
    assert result.is_error
    assert n._delegation_depth == 0  # restored


# --- argument validation -------------------------------------------


def test_missing_task_type_returns_error() -> None:
    n = _RecordingNation()
    exec_fn = make_delegate_executor(n, parent_agent_id="ant-parent")
    result = asyncio.run(exec_fn(ToolCall(
        id="d1", name="delegate_task",
        arguments={"prompt": "x"},
    )))
    assert result.is_error
    assert "task_type" in result.content


def test_missing_prompt_returns_error() -> None:
    n = _RecordingNation()
    exec_fn = make_delegate_executor(n, parent_agent_id="ant-parent")
    result = asyncio.run(exec_fn(ToolCall(
        id="d1", name="delegate_task",
        arguments={"task_type": "x"},
    )))
    assert result.is_error
    assert "prompt" in result.content


# --- low-score result surfaces as is_error -------------------------


def test_low_success_score_propagates_as_error() -> None:
    """If the child citizen failed (success_score < 0.5), the
    tool result is marked is_error so the parent loop knows to
    consider an alternative."""
    n = _RecordingNation()
    orig_run = n.run

    async def failing_run(task_type, prompt, *, forbid=None, **kwargs):
        result = await orig_run(task_type, prompt, forbid=forbid, **kwargs)
        result.success_score = 0.0
        return result

    n.run = failing_run  # type: ignore[assignment]
    exec_fn = make_delegate_executor(n, parent_agent_id="ant-parent")
    result = asyncio.run(exec_fn(ToolCall(
        id="d1", name="delegate_task",
        arguments={"task_type": "x", "prompt": "y"},
    )))
    assert result.is_error


# --- tool spec sanity ----------------------------------------------


def test_delegate_task_spec_serializes() -> None:
    """The DELEGATE_TASK spec converts to both provider formats."""
    openai = DELEGATE_TASK.to_openai_format()
    assert openai["function"]["name"] == "delegate_task"
    assert "task_type" in openai["function"]["parameters"]["properties"]
    assert "prompt" in openai["function"]["parameters"]["properties"]

    anthropic = DELEGATE_TASK.to_anthropic_format()
    assert anthropic["name"] == "delegate_task"


def test_builtin_tools_can_include_delegate() -> None:
    """The builtin_tools toggle exposes delegate_task only when asked."""
    from anthill.core.tools_protocol import builtin_tools

    without = [t.name for t in builtin_tools()]
    assert "delegate_task" not in without

    with_delegate = [t.name for t in builtin_tools(include_delegate=True)]
    assert "delegate_task" in with_delegate
