"""0.2.29 — Native tool_use protocol.

Until now anthill citizens emitted [[bash:CMD]] / [[browser:CMD]]
markers in their text output. We parsed those out post-hoc and
ran them. That works across providers but relies on instruction-
following — deepseek and minimax demonstrated this is unreliable.

This module adds the parallel path: native tool_use API. The
model emits structured tool calls via the provider's native
mechanism (OpenAI's `tool_calls` field, Anthropic's `tool_use`
content blocks). The model is TRAINED for this format, so it
reliably calls tools when given them.

Vocabulary (kept tight — these three are all we need):
  ToolSpec    — the declaration: name, description, input_schema
  ToolCall    — what the model emitted: id, name, arguments
  ToolResult  — what we got back: tool_call_id, content, is_error

Provider adapters live in models/*.py. The shapes here are
provider-neutral; conversion happens at request/response time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


# Hard cap on tool-loop iterations within a single citizen ask. The
# whole point of native tool_use is multi-step autonomy, but a model
# stuck in a planning loop ("let me check, let me check, let me
# check") would burn budget without progress. 8 is enough for a
# typical functional-test flow (login → navigate → click → assert)
# while keeping a budget cap.
DEFAULT_MAX_TOOL_ITERATIONS: int = 8


@dataclass
class ToolSpec:
    """One tool the model is allowed to call.

    `input_schema` is a JSON Schema dict. Providers convert it to
    their native format (Anthropic uses `input_schema` directly,
    OpenAI wraps it as `parameters` inside `function`).
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    def to_openai_format(self) -> dict[str, Any]:
        """OpenAI / DeepSeek / Moonshot / Minimax / Qwen tool shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_anthropic_format(self) -> dict[str, Any]:
        """Anthropic Messages API tool shape."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolCall:
    """One tool invocation emitted by the model.

    `id` is the provider-supplied call ID we'll echo back when
    submitting the result so the model can correlate. Some
    providers (older OpenAI) didn't include IDs; we generate one
    locally in that case so anthill's downstream logic always has
    a stable handle.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """What we send back to the model after running a tool."""

    tool_call_id: str
    content: str
    is_error: bool = False


# Executor signature — given a ToolCall, return a ToolResult. Async
# because shell exec and browser ops are async-friendly.
ToolExecutor = Callable[[ToolCall], Awaitable[ToolResult]]


# --- built-in tool specs ---------------------------------------------


BASH_RUN = ToolSpec(
    name="bash_run",
    description=(
        "Run a shell command on the king's machine. Returns stdout, "
        "stderr, exit code. Use this for ANY action — checking system "
        "state, network reachability, git operations, file reads, "
        "running tests, etc. There is a 30s timeout per command. "
        "Output is truncated to 64KB. NEVER claim 'I don't have "
        "shell access' — you do, this is it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "cmd": {
                "type": "string",
                "description": (
                    "The shell command to run. Plain bash syntax. "
                    "Pipes, redirects, &&, ||, $(...) all work."
                ),
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Optional timeout in seconds. Default 30. Max 300."
                ),
            },
        },
        "required": ["cmd"],
    },
)


# Browser tool — exposes the same actions [[browser:ACTION ARGS]]
# does, but via a structured schema the model can call natively.
# Registered conditionally in 0.2.30 (when a BrowserSession is
# available); keeping the spec here for forward reference.
BROWSER_ACTION = ToolSpec(
    name="browser_action",
    description=(
        "Drive the king's persistent browser. Use to actually USE "
        "websites — click buttons, fill forms, screenshot pages. "
        "The session preserves cookies/state across calls. NEVER "
        "describe steps; just call this with action='goto', "
        "action='click', etc."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "goto", "click", "fill", "press", "text",
                    "wait", "screenshot", "url", "reload", "evaluate",
                ],
                "description": "Which action to perform.",
            },
            "args": {
                "type": "string",
                "description": (
                    "Action-specific argument string. "
                    "goto: URL. "
                    "click: SELECTOR. "
                    "fill: 'SELECTOR VALUE'. "
                    "press: 'SELECTOR KEY'. "
                    "text: SELECTOR (or empty for body). "
                    "wait: 'SELECTOR [visible|hidden]'. "
                    "screenshot: optional NAME. "
                    "url: empty. "
                    "reload: empty. "
                    "evaluate: JS expression."
                ),
            },
        },
        "required": ["action"],
    },
)


def builtin_tools(
    include_browser: bool = False,
    include_kanban: bool = False,
    include_delegate: bool = False,
) -> list[ToolSpec]:
    """The default tool set.

    `include_browser` (0.2.30+) registers `browser_action`.
    `include_kanban` (0.2.31+) registers `kanban_*` tools.
    `include_delegate` (0.2.32+) registers `delegate_task` for
    multi-agent collaboration.
    """
    tools: list[ToolSpec] = [BASH_RUN]
    if include_browser:
        tools.append(BROWSER_ACTION)
    if include_kanban:
        from anthill.core.kanban_tools import KANBAN_TOOLS
        tools.extend(KANBAN_TOOLS)
    if include_delegate:
        from anthill.core.delegate import DELEGATE_TASK
        tools.append(DELEGATE_TASK)
    return tools
