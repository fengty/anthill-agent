"""0.2.29 — Multi-turn agentic loop.

The runner is provider-neutral, so we test with a FakeProvider that
returns canned ModelResponses. This exercises the full ReAct flow
without touching a real LLM.

Tests cover:
  - 0 tool calls → return immediately (natural finish)
  - 1 tool call → execute → next call → return (two-turn loop)
  - Multiple sequential tool calls (ping then curl)
  - Tool returning error doesn't break loop (model can recover)
  - max_iterations cap prevents runaways
  - Executor crash → ToolResult(is_error=True), not propagated
  - Token usage accumulates across iterations
  - bash_executor: real shell exec via safe_run
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from anthill.core.agent_loop import (
    AgentLoopResult,
    run_agent_loop,
)
from anthill.core.tool_executors import bash_executor, dispatch_tool_call
from anthill.core.tools_protocol import (
    BASH_RUN,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from anthill.models.base import ModelProvider, ModelResponse


# --- a fake provider ---------------------------------------------------


class _FakeProvider(ModelProvider):
    """Returns canned ModelResponses in sequence. Each call consumes
    one from the list. Token counts are deterministic for testing
    accumulation."""

    name = "fake"

    def __init__(self, responses: list[ModelResponse]):
        self._responses = list(responses)
        self.calls = 0  # how many times complete_with_messages got called

    async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
        # Not used by the loop, but ABC requires it.
        return ModelResponse(text="(complete fallback)", model="fake")

    async def complete_with_messages(
        self,
        messages,
        *,
        system=None,
        tools=None,
        max_tokens=4096,
        temperature=0.7,
    ):
        self.calls += 1
        if not self._responses:
            return ModelResponse(text="(no more responses)", model="fake")
        return self._responses.pop(0)


def _r(text: str, *, tool_calls=None, in_tok=10, out_tok=5, fr="stop"):
    """Build a ModelResponse quickly."""
    return ModelResponse(
        text=text,
        model="fake",
        input_tokens=in_tok,
        output_tokens=out_tok,
        finish_reason=fr,
        tool_calls=tool_calls or [],
    )


def _tc(call_id: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


# --- loop termination ------------------------------------------------


def test_natural_finish_one_turn() -> None:
    """Model returns text with no tool_calls → loop exits after 1 turn."""
    provider = _FakeProvider([_r("here's your answer")])

    async def executor(call):
        return ToolResult(tool_call_id=call.id, content="never called")

    result = asyncio.run(run_agent_loop(
        provider,
        system=None,
        initial_user_message="hello",
        tools=[BASH_RUN],
        executor=executor,
    ))
    assert result.iterations == 1
    assert result.tool_calls_made == 0
    assert result.stopped_for == "natural"
    assert result.final_text == "here's your answer"


def test_two_turn_loop_with_one_tool_call() -> None:
    """Turn 1: model emits tool_call. Turn 2: model returns text. Loop ends."""
    provider = _FakeProvider([
        _r("checking ports", tool_calls=[_tc("c1", "bash_run", {"cmd": "echo hi"})], fr="tool_calls"),
        _r("found nothing"),
    ])

    async def executor(call):
        return ToolResult(tool_call_id=call.id, content="hi\n")

    result = asyncio.run(run_agent_loop(
        provider,
        system=None,
        initial_user_message="check ports",
        tools=[BASH_RUN],
        executor=executor,
    ))
    assert result.iterations == 2
    assert result.tool_calls_made == 1
    assert result.final_text == "found nothing"


def test_multiple_sequential_tool_calls() -> None:
    """Model emits tool, sees result, emits another, sees result, finishes."""
    provider = _FakeProvider([
        _r("step 1", tool_calls=[_tc("c1", "bash_run", {"cmd": "ls"})]),
        _r("step 2", tool_calls=[_tc("c2", "bash_run", {"cmd": "pwd"})]),
        _r("done"),
    ])

    seen: list[str] = []

    async def executor(call):
        seen.append(call.arguments["cmd"])
        return ToolResult(tool_call_id=call.id, content="ok")

    result = asyncio.run(run_agent_loop(
        provider,
        system=None,
        initial_user_message="run two",
        tools=[BASH_RUN],
        executor=executor,
    ))
    assert seen == ["ls", "pwd"]
    assert result.iterations == 3
    assert result.tool_calls_made == 2


def test_max_iterations_caps_runaway_loop() -> None:
    """Model that always emits a tool call → loop stops at max_iters."""
    # Build a provider that returns tool_calls indefinitely.
    def gen_responses(n):
        return [
            _r(f"step {i}", tool_calls=[_tc(f"c{i}", "bash_run", {"cmd": "echo x"})])
            for i in range(n)
        ]
    provider = _FakeProvider(gen_responses(20))

    async def executor(call):
        return ToolResult(tool_call_id=call.id, content="x")

    result = asyncio.run(run_agent_loop(
        provider,
        system=None,
        initial_user_message="loop forever",
        tools=[BASH_RUN],
        executor=executor,
        max_iterations=3,
    ))
    assert result.iterations == 3
    assert result.stopped_for == "max_iters"


def test_token_usage_accumulates() -> None:
    """Total tokens across iterations sum correctly."""
    provider = _FakeProvider([
        _r("a", tool_calls=[_tc("c1", "bash_run", {"cmd": "x"})], in_tok=100, out_tok=20),
        _r("done", in_tok=120, out_tok=30),
    ])

    async def executor(call):
        return ToolResult(tool_call_id=call.id, content="ok")

    result = asyncio.run(run_agent_loop(
        provider, system=None, initial_user_message="x",
        tools=[BASH_RUN], executor=executor,
    ))
    assert result.input_tokens == 100 + 120
    assert result.output_tokens == 20 + 30


# --- executor errors don't break the loop -----------------------------


def test_executor_crash_becomes_tool_error() -> None:
    """If the executor raises, the loop catches it and feeds a
    ToolResult(is_error=True) back to the model so it can decide
    what to do."""
    provider = _FakeProvider([
        _r("step 1", tool_calls=[_tc("c1", "bash_run", {"cmd": "x"})]),
        _r("ok, I'll try something else"),
    ])

    async def crashing_executor(call):
        raise RuntimeError("simulated executor crash")

    result = asyncio.run(run_agent_loop(
        provider, system=None, initial_user_message="x",
        tools=[BASH_RUN], executor=crashing_executor,
    ))
    # Loop survived, reached iteration 2.
    assert result.iterations == 2
    # The tool error message was added to messages so the model saw it.
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "executor crashed" in tool_messages[0]["content"]


# --- progress callbacks fire correctly -------------------------------


def test_progress_callbacks_fire_in_order() -> None:
    """on_iteration_start, on_tool_call, on_tool_result fire in the
    sequence the REPL needs to render the loop live."""
    provider = _FakeProvider([
        _r("step 1", tool_calls=[_tc("c1", "bash_run", {"cmd": "x"})]),
        _r("done"),
    ])

    async def executor(call):
        return ToolResult(tool_call_id=call.id, content="ok")

    events: list[str] = []

    asyncio.run(run_agent_loop(
        provider, system=None, initial_user_message="x",
        tools=[BASH_RUN], executor=executor,
        on_iteration_start=lambda i, ms: events.append(f"iter-{i}"),
        on_tool_call=lambda tc: events.append(f"call-{tc.name}"),
        on_tool_result=lambda tc, r: events.append(f"result-{tc.id}"),
    ))
    assert events == [
        "iter-1", "call-bash_run", "result-c1", "iter-2",
    ]


# --- the bash_executor really runs shell ----------------------------


def test_bash_executor_runs_real_shell() -> None:
    """Round-trip: ToolCall → safe_run → ToolResult containing real
    stdout. No LLM in the loop."""
    call = ToolCall(id="t1", name="bash_run", arguments={"cmd": "echo hello"})
    result = asyncio.run(bash_executor(call))
    assert not result.is_error
    assert "hello" in result.content
    assert result.tool_call_id == "t1"


def test_bash_executor_rejects_missing_cmd() -> None:
    call = ToolCall(id="t1", name="bash_run", arguments={})
    result = asyncio.run(bash_executor(call))
    assert result.is_error
    assert "cmd" in result.content


def test_bash_executor_flags_nonzero_as_error() -> None:
    """exit 1 → is_error so the model treats it as a failed step."""
    call = ToolCall(id="t1", name="bash_run", arguments={"cmd": "false"})
    result = asyncio.run(bash_executor(call))
    assert result.is_error


def test_dispatch_unknown_tool_returns_error() -> None:
    """Bad tool name → friendly error, not a crash."""
    call = ToolCall(id="t1", name="hammertime", arguments={})
    result = asyncio.run(dispatch_tool_call(call))
    assert result.is_error
    assert "Unknown tool" in result.content


# --- tool_spec serialization -----------------------------------------


def test_bash_run_spec_serializes_to_openai_format() -> None:
    """The bash_run tool can be sent to an OpenAI-compatible API."""
    fmt = BASH_RUN.to_openai_format()
    assert fmt["type"] == "function"
    assert fmt["function"]["name"] == "bash_run"
    # The cmd parameter must be in the schema.
    assert "cmd" in fmt["function"]["parameters"]["properties"]


def test_bash_run_spec_serializes_to_anthropic_format() -> None:
    """Same tool, different shape for Anthropic Messages API."""
    fmt = BASH_RUN.to_anthropic_format()
    assert fmt["name"] == "bash_run"
    # Anthropic uses input_schema directly (no nested 'function').
    assert "input_schema" in fmt
    assert "cmd" in fmt["input_schema"]["properties"]
