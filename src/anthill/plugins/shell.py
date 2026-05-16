"""Shell plugin — run a shell command with safety rails.

Off by default. Has to be explicitly enabled via
ANTHILL_PLUGIN_SHELL_ENABLED=1 because giving an LLM unrestricted shell
access is exactly how you end up with a famous incident.

When enabled:
    - Runs the command inside ANTHILL_PLUGIN_WORKSPACE (cwd)
    - Hard 30s timeout
    - Captures stdout + stderr, truncated to 8KB each
    - Refuses anything starting with rm -rf, sudo, or piped curl|bash
      (basic guardrail, not a sandbox)
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from anthill.plugins.base import Plugin, PluginResult


_DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+-rf\s+/"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bcurl\b.*\|\s*(bash|sh)"),
    re.compile(r"\bwget\b.*\|\s*(bash|sh)"),
    re.compile(r":\(\)\s*\{.*\};"),  # fork bomb shape
    re.compile(r"\bmkfs\b"),
    re.compile(r"/dev/sd[a-z]"),
]


def _workspace_root() -> Path:
    raw = os.getenv("ANTHILL_PLUGIN_WORKSPACE")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".anthill" / "workspace"


def _is_dangerous(cmd: str) -> str | None:
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(cmd):
            return pattern.pattern
    return None


class ShellPlugin(Plugin):
    name = "shell"
    description = "Run a shell command in the workspace (off by default)."

    async def call(self, *, command: str, timeout: float = 30.0, **_: Any) -> PluginResult:
        if os.getenv("ANTHILL_PLUGIN_SHELL_ENABLED", "").lower() not in (
            "1", "true", "yes", "on"
        ):
            return PluginResult(
                output=None,
                ok=False,
                error="shell plugin disabled; set ANTHILL_PLUGIN_SHELL_ENABLED=1 to enable.",
            )

        if not command.strip():
            return PluginResult(output=None, ok=False, error="empty command")

        danger = _is_dangerous(command)
        if danger:
            return PluginResult(
                output=None,
                ok=False,
                error=f"refused: matches dangerous pattern {danger!r}",
            )

        workspace = _workspace_root()
        workspace.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return PluginResult(output=None, ok=False, error=f"timeout after {timeout}s")
        except Exception as e:  # noqa: BLE001
            return PluginResult(output=None, ok=False, error=str(e))

        # Truncate large output so we don't blow context downstream.
        out_text = stdout.decode("utf-8", errors="replace")[:8192]
        err_text = stderr.decode("utf-8", errors="replace")[:8192]

        return PluginResult(
            output={"stdout": out_text, "stderr": err_text, "exit_code": proc.returncode},
            metadata={"cwd": str(workspace), "exit_code": proc.returncode},
            ok=proc.returncode == 0,
        )
