"""0.2.33 — on_tool_call / on_tool_result reach the REPL during the
agent loop.

Without this plumbing, the multi-turn agent loop is invisible to
the user: model emits 3 tool calls inside one subtask, the user
sees a long silent pause then a final answer. The callbacks fire
INSIDE the loop so the REPL can render "🐚 running:" as each call
happens.

Tests verify the callback signatures flow through:
  REPL → nation.ask → execute_plan → _run_one_subtask → nation.run
       → agent.execute → run_agent_loop → on_tool_call / on_tool_result

We mock the provider so no LLM runs; we verify the callback wiring.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from anthill.core.agent import Agent
from anthill.core.executor import execute_plan
from anthill.core.nation import Nation
from anthill.core.scout import Plan, Subtask
from anthill.core.tools_protocol import ToolCall
from anthill.models.base import ModelProvider, ModelResponse


class _ToolEmittingProvider(ModelProvider):
    """Provider that emits one tool call then a final text response.

    Lets us drive a 2-turn agent_loop without a real model."""

    name = "fake-tools"

    def __init__(self):
        self.turn = 0

    async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
        return ModelResponse(text="(fallback)", model="fake")

    async def complete_with_messages(
        self, messages, *, system=None, tools=None,
        max_tokens=4096, temperature=0.7,
    ):
        self.turn += 1
        if self.turn == 1:
            # First turn: ask to run bash.
            return ModelResponse(
                text="let me check",
                model="fake-tools",
                input_tokens=20,
                output_tokens=10,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="tc-1", name="bash_run",
                        arguments={"cmd": "echo hello"},
                    )
                ],
            )
        # Subsequent turn: final answer.
        return ModelResponse(
            text="all done",
            model="fake-tools",
            input_tokens=30,
            output_tokens=5,
            finish_reason="stop",
        )


def _build_test_nation(tmp_path: Path) -> Nation:
    """A nation in agentic mode with one citizen using our fake provider."""
    n = Nation(name="t")
    agent = Agent(id="ant-x", model="fake-tools")
    # Inject the fake provider directly so agent.execute uses it.
    fake = _ToolEmittingProvider()
    agent._provider = fake  # type: ignore[attr-defined]
    # Force the provider lookup to return our fake.
    agent._get_provider = lambda: fake  # type: ignore[attr-defined]
    n.agents = [agent]
    n._anthill_home = tmp_path  # type: ignore[attr-defined]
    n.agentic_mode = True  # type: ignore[attr-defined]
    return n


def test_on_tool_call_fires_during_execute_plan(tmp_path: Path) -> None:
    """End-to-end: call execute_plan with on_tool_call/result and
    verify both callbacks received the bash_run invocation."""
    nation = _build_test_nation(tmp_path)
    plan = Plan(subtasks=[Subtask("research", "do x", [])])

    seen_calls: list[ToolCall] = []
    seen_results: list[tuple[ToolCall, object]] = []

    outcomes = asyncio.run(execute_plan(
        plan,
        nation,
        on_tool_call=lambda tc: seen_calls.append(tc),
        on_tool_result=lambda tc, tr: seen_results.append((tc, tr)),
    ))

    # The bash_run call surfaced via the callback.
    assert len(seen_calls) == 1
    assert seen_calls[0].name == "bash_run"
    assert seen_calls[0].arguments["cmd"] == "echo hello"

    # And the matching result fired.
    assert len(seen_results) == 1
    tc, tr = seen_results[0]
    assert tc.name == "bash_run"
    # The bash_executor really ran echo, so we see "hello" in the result.
    assert "hello" in tr.content

    # The plan completed normally.
    assert len(outcomes) == 1
    assert outcomes[0].final is not None


def test_nation_ask_forwards_tool_callbacks(tmp_path: Path) -> None:
    """nation.ask is the public entry point — verify it forwards
    on_tool_call/result through execute_plan to the agent loop."""
    nation = _build_test_nation(tmp_path)

    seen: list[str] = []

    # Use pre_plan to skip Scout (no LLM for Scout, since we faked
    # only the citizen provider).
    plan = Plan(subtasks=[Subtask("research", "do x", [])])

    asyncio.run(nation.ask(
        "test request",
        pre_plan=plan,
        nation_dir=tmp_path,
        on_tool_call=lambda tc: seen.append(f"call:{tc.name}"),
        on_tool_result=lambda tc, tr: seen.append(f"result:{tc.name}"),
    ))

    # Both fired, in order.
    assert seen == ["call:bash_run", "result:bash_run"]


def test_callbacks_optional_default_to_no_op(tmp_path: Path) -> None:
    """Existing callers (headless, tests, batch) don't pass these
    callbacks. The loop must work fine without them."""
    nation = _build_test_nation(tmp_path)
    plan = Plan(subtasks=[Subtask("research", "do x", [])])

    # No callbacks passed at all.
    outcomes = asyncio.run(execute_plan(plan, nation))

    assert len(outcomes) == 1
    assert outcomes[0].final is not None
