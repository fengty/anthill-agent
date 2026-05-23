"""0.2.29 — Multi-turn agentic loop.

Until now anthill ran one model call per subtask:
  prompt → text → (parse markers post-hoc, run them) → done

Hermes / Claude Code / Cursor / Cline all use a different shape:
  prompt → model emits tool_calls → execute → append results to
  messages → call model again with the new history → repeat until
  model returns no more tool_calls.

That second shape is what makes them "agents" instead of
"one-shot tool runners." The model can see the result of step 1
before deciding step 2. Functional testing (login → check
dashboard → assert text) NEEDS this — step N depends on step N-1's
output.

This module is the runner. Given:
  - a provider that supports complete_with_messages
  - a list of ToolSpec
  - an executor that maps ToolCall → ToolResult
  - a starting user message

it runs the loop until the model is done or hits max_iterations.

The runner is transport-neutral — the same loop drives a REPL,
a background job, or a /test session. Progress callbacks let
the caller render the loop as it unfolds.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from anthill.core.tools_protocol import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from anthill.models.base import ModelProvider, ModelResponse


@dataclass
class AgentLoopResult:
    """Outcome of one agent loop run."""

    final_text: str                          # last assistant message's text
    iterations: int                          # how many model turns ran
    tool_calls_made: int                     # total tool calls executed
    input_tokens: int                        # cumulative across iterations
    output_tokens: int
    finish_reason: Optional[str]             # last response's finish_reason
    messages: list = field(default_factory=list)  # full conversation
    stopped_for: str = "natural"             # natural / max_iters / error

    @property
    def short_summary(self) -> str:
        return (
            f"{self.iterations} turn(s), {self.tool_calls_made} tool call(s), "
            f"{self.input_tokens}↓ {self.output_tokens}↑ tok"
        )


# Callback signatures for the REPL to observe what's happening.
# All are sync (the REPL renders synchronously); async work happens
# inside the loop, not in callbacks.
OnIterationStart = Callable[[int, list], None]   # (iter_num, messages_so_far)
OnToolCall = Callable[[ToolCall], None]          # before executing
OnToolResult = Callable[[ToolCall, ToolResult], None]  # after
OnAssistantText = Callable[[str], None]          # streamed-ish text


async def run_agent_loop(
    provider: ModelProvider,
    *,
    system: str | None,
    initial_user_message: str,
    tools: list[ToolSpec],
    executor: Callable[[ToolCall], Awaitable[ToolResult]],
    max_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    on_iteration_start: OnIterationStart | None = None,
    on_tool_call: OnToolCall | None = None,
    on_tool_result: OnToolResult | None = None,
    on_assistant_text: OnAssistantText | None = None,
) -> AgentLoopResult:
    """Run the ReAct loop until the model stops calling tools.

    Termination conditions:
      1. Model returns a response with no tool_calls (natural finish).
      2. We hit `max_iterations` (model is stuck in a loop or doing
         too much in one ask).
      3. Provider raises — re-raised so caller knows.

    The function mutates `messages` in place across iterations so a
    caller observing via on_iteration_start can see the growing
    history. The same list is returned in `AgentLoopResult.messages`.
    """
    messages: list[dict] = [
        {"role": "user", "content": initial_user_message},
    ]
    total_in = 0
    total_out = 0
    tool_count = 0
    last_text = ""
    last_finish_reason: Optional[str] = None

    for it in range(1, max_iterations + 1):
        if on_iteration_start is not None:
            on_iteration_start(it, messages)

        response: ModelResponse = await provider.complete_with_messages(
            messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        total_in += response.input_tokens
        total_out += response.output_tokens
        last_text = response.text or ""
        last_finish_reason = response.finish_reason

        if response.text and on_assistant_text is not None:
            on_assistant_text(response.text)

        # No tool calls → model is done.
        if not response.tool_calls:
            return AgentLoopResult(
                final_text=last_text,
                iterations=it,
                tool_calls_made=tool_count,
                input_tokens=total_in,
                output_tokens=total_out,
                finish_reason=last_finish_reason,
                messages=messages,
                stopped_for="natural",
            )

        # Append the assistant turn (with tool_calls) so the model
        # sees its own decision in next iteration.
        assistant_msg: dict = {
            "role": "assistant",
            "content": response.text or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        # Re-serialize args as JSON string (OpenAI shape).
                        "arguments": _json_dumps_safely(tc.arguments),
                    },
                }
                for tc in response.tool_calls
            ],
        }
        messages.append(assistant_msg)

        # Execute each tool call, append its result.
        for tc in response.tool_calls:
            if on_tool_call is not None:
                on_tool_call(tc)
            try:
                result = await executor(tc)
            except Exception as e:  # noqa: BLE001 — convert into a tool error
                result = ToolResult(
                    tool_call_id=tc.id,
                    content=f"executor crashed: {type(e).__name__}: {e}",
                    is_error=True,
                )
            tool_count += 1
            if on_tool_result is not None:
                on_tool_result(tc, result)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result.content,
            })

    # Hit the iteration cap.
    return AgentLoopResult(
        final_text=last_text or "(loop hit max_iterations without final answer)",
        iterations=max_iterations,
        tool_calls_made=tool_count,
        input_tokens=total_in,
        output_tokens=total_out,
        finish_reason=last_finish_reason,
        messages=messages,
        stopped_for="max_iters",
    )


def _json_dumps_safely(obj) -> str:
    """Serialize tool-call arguments back to JSON for OpenAI shape.

    Some args might already be a string (rare malformed cases) — we
    pass those through. Anything else json.dumps with a fallback.
    """
    import json

    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(str(obj), ensure_ascii=False)
