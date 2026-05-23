"""0.2.29 — Executors that map ToolCall → ToolResult.

The agent loop is transport/protocol-neutral: it just calls
`executor(tool_call)` and expects a ToolResult back. This module
provides the actual implementations:

  - `bash_executor` — runs bash_run via safe_run
  - `browser_executor` — runs browser_action via BrowserSession (0.2.30)
  - `compose_executors` — dispatch by tool name to the right impl

Keep these thin. Errors should be returned as ToolResult(is_error=True)
not raised — the model needs to SEE the error to decide what to do
next, not have the whole loop crash.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from anthill.core.shell import ShellResult, safe_run
from anthill.core.tools_protocol import ToolCall, ToolResult


def _format_shell_result(cmd: str, result: ShellResult) -> str:
    """Render a ShellResult into the text the model will see as
    tool output. Keep it compact but informative — the model needs
    enough signal to decide the next step."""
    lines: list[str] = []
    if result.blocked_reason:
        return f"REFUSED: {result.blocked_reason}\n(command: {cmd})"
    if result.command != cmd:
        lines.append(f"# auto-capped to: {result.command}")
    if result.timed_out:
        lines.append(f"# TIMED OUT after {result.duration_seconds:.1f}s")
    else:
        lines.append(f"# exit {result.returncode} in {result.duration_seconds:.2f}s")
    if result.stdout.strip():
        lines.append("--- stdout ---")
        lines.append(result.stdout.rstrip())
    if result.stderr.strip():
        lines.append("--- stderr ---")
        lines.append(result.stderr.rstrip())
    return "\n".join(lines)


async def bash_executor(call: ToolCall) -> ToolResult:
    """Run a bash_run tool call via the existing safe_run primitive.

    Reuses 0.2.19's safety guards (hard-deny list, auto-cap, hard
    timeout, output truncation) so native tool calls get the same
    treatment as [[bash:]] markers.
    """
    args = call.arguments or {}
    cmd = args.get("cmd")
    if not cmd or not isinstance(cmd, str):
        return ToolResult(
            tool_call_id=call.id,
            content="bash_run requires a `cmd` string argument.",
            is_error=True,
        )
    timeout_val = args.get("timeout", 30)
    try:
        timeout = float(timeout_val)
    except (TypeError, ValueError):
        timeout = 30.0
    # Cap user-requested timeout — let the model bump it up to 300
    # for genuinely long jobs but no further.
    timeout = max(1.0, min(timeout, 300.0))

    result = safe_run(cmd, timeout=timeout)
    is_error = (
        result.blocked_reason is not None
        or result.timed_out
        or result.returncode != 0
    )
    return ToolResult(
        tool_call_id=call.id,
        content=_format_shell_result(cmd, result),
        is_error=is_error,
    )


# Browser executor lives here too — 0.2.30 will wire it to a real
# BrowserSession via nation._browser_session. Stubbed signature in
# place so dispatch code is forward-compatible.
async def browser_executor(call: ToolCall) -> ToolResult:
    """Stub for 0.2.30. Right now we tell the model to use the
    [[browser:]] marker syntax instead. Full native impl when
    BrowserSession integration lands."""
    return ToolResult(
        tool_call_id=call.id,
        content=(
            "browser_action via native tool_use isn't wired yet "
            "(0.2.30). Use the [[browser:ACTION ARGS]] marker syntax "
            "in your text response instead — it executes via the "
            "same Playwright session."
        ),
        is_error=True,
    )


# Dispatch by tool name. Used as the `executor` arg to run_agent_loop.
# New tools (kanban_*, delegate, etc.) register here in 0.2.31+.
async def dispatch_tool_call(call: ToolCall) -> ToolResult:
    """Look up the right executor for `call.name` and run it.

    Unknown tool name → ToolResult with is_error=True telling the
    model "no such tool" so it can correct and retry.
    """
    name = (call.name or "").strip()
    if name == "bash_run":
        return await bash_executor(call)
    if name == "browser_action":
        return await browser_executor(call)
    return ToolResult(
        tool_call_id=call.id,
        content=(
            f"Unknown tool: {name!r}. Available tools: bash_run, "
            f"browser_action. Use bash_run for shell commands."
        ),
        is_error=True,
    )
