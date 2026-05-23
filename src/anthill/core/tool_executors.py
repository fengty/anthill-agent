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


# Module-level browser session — shared across all native browser_action
# tool calls in this REPL process. Lazily started on first use.
_browser_session = None


async def _get_or_create_browser_session():
    """Return a started BrowserSession, creating one on first call.
    Lifetime is process-wide; the chromium dies when anthill exits."""
    global _browser_session
    if _browser_session is not None:
        return _browser_session
    from anthill.core.browser_drive import BrowserSession
    sess = BrowserSession(state_dir=None, headless=False)
    start = await sess.start()
    if not start.ok:
        return None
    _browser_session = sess
    return _browser_session


async def browser_executor(call: ToolCall) -> ToolResult:
    """0.2.30 — Real browser driving via the persistent Playwright
    session. Mirrors the [[browser:ACTION ARGS]] semantics but via
    native tool_use."""
    args = call.arguments or {}
    action = (args.get("action") or "").strip().lower()
    arg_str = args.get("args", "") or ""
    if not action:
        return ToolResult(
            tool_call_id=call.id,
            content="browser_action requires `action` (goto/click/fill/...)",
            is_error=True,
        )
    sess = await _get_or_create_browser_session()
    if sess is None:
        return ToolResult(
            tool_call_id=call.id,
            content=(
                "Playwright is not installed. Run /setup browser in the "
                "REPL or `anthill setup browser` from the shell, then "
                "retry."
            ),
            is_error=True,
        )
    br = await sess.execute(action, arg_str)
    content_lines = [f"# {br.short_summary}"]
    if br.value is not None and br.value != "":
        content_lines.append(f"--- result ---")
        content_lines.append(str(br.value))
    return ToolResult(
        tool_call_id=call.id,
        content="\n".join(content_lines),
        is_error=not br.ok,
    )


# Dispatch by tool name. Used as the `executor` arg to run_agent_loop.
# 0.2.31 — registers kanban_* tools when an anthill home is bound.
async def dispatch_tool_call(call: ToolCall) -> ToolResult:
    """Look up the right executor for `call.name` and run it.

    Unknown tool name → ToolResult with is_error=True telling the
    model "no such tool" so it can correct and retry.

    For kanban_*: this default dispatch returns an error because
    no home dir is bound. The REPL uses `make_dispatch_with_kanban`
    to get a flavor of dispatch that knows where the board lives.
    """
    name = (call.name or "").strip()
    if name == "bash_run":
        return await bash_executor(call)
    if name == "browser_action":
        return await browser_executor(call)
    if name.startswith("kanban_"):
        return ToolResult(
            tool_call_id=call.id,
            content=(
                "kanban_* tools need a bound anthill home dir. "
                "Use make_dispatch_with_kanban() instead of the "
                "default dispatch."
            ),
            is_error=True,
        )
    return ToolResult(
        tool_call_id=call.id,
        content=(
            f"Unknown tool: {name!r}. Available tools: bash_run, "
            f"browser_action, kanban_*. Use bash_run for shell commands."
        ),
        is_error=True,
    )


def make_dispatch_with_kanban(
    home,
    default_assignee: str | None = None,
    nation=None,
    vision_provider=None,
    vision_model_name: str = "",
):
    """0.2.31 — dispatch that knows where the kanban DB lives.
    0.2.32 — also wires delegate_task if a nation is provided.
    0.2.40 — also wires visual_check if a vision_provider is given.

    Returns an async fn matching the agent_loop executor signature.
    """
    from anthill.core.kanban_tools import make_kanban_executors
    kanban_dispatch, _handlers = make_kanban_executors(home, default_assignee)

    delegate_exec = None
    if nation is not None:
        from anthill.core.delegate import make_delegate_executor
        delegate_exec = make_delegate_executor(nation, default_assignee or "")

    visual_exec = None
    if vision_provider is not None:
        from anthill.core.vision import make_visual_check_executor
        visual_exec = make_visual_check_executor(
            vision_provider=vision_provider,
            vision_model_name=vision_model_name,
        )

    async def dispatch(call: ToolCall) -> ToolResult:
        name = (call.name or "").strip()
        if name == "bash_run":
            return await bash_executor(call)
        if name == "browser_action":
            return await browser_executor(call)
        if name.startswith("kanban_"):
            return await kanban_dispatch(call)
        if name == "delegate_task" and delegate_exec is not None:
            return await delegate_exec(call)
        if name == "visual_check" and visual_exec is not None:
            return await visual_exec(call)
        if name == "visual_check":
            # Registered as a tool but no vision provider — give the
            # citizen a clear path forward instead of "unknown tool."
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "visual_check needs a vision model. Run:\n"
                    "  anthill values set vision_model <model>"
                ),
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Unknown tool: {name!r}. Available tools: bash_run, "
                f"browser_action, kanban_*, delegate_task, visual_check."
            ),
            is_error=True,
        )

    return dispatch
