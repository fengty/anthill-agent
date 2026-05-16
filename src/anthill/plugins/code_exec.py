"""Python code execution — run a snippet inside the workspace.

Same safety posture as the shell plugin: OFF by default, requires
ANTHILL_PLUGIN_CODE_EXEC_ENABLED=1 to turn on. The intent is
short, deterministic computations the agent uses to verify its own
output ('what's 12.7% of 8540', 'fit a linear regression to this
list of points') — not a general programming environment.

Mechanism: write the code to a temp file inside the workspace, run it
with the current Python interpreter via subprocess, capture stdout and
stderr, hard-timeout 15s. We do NOT run user code in-process: a stray
import or infinite loop would take the daemon down with it.

Output cap: 8KB stdout + 8KB stderr. Larger payloads truncate; the
plugin still returns ok=True if exit_code == 0.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from anthill.plugins.base import Plugin, PluginResult


def _workspace_root() -> Path:
    raw = os.getenv("ANTHILL_PLUGIN_WORKSPACE")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".anthill" / "workspace"


def _enabled() -> bool:
    return os.getenv("ANTHILL_PLUGIN_CODE_EXEC_ENABLED", "").lower() in (
        "1", "true", "yes", "on"
    )


class CodeExecPlugin(Plugin):
    name = "code_exec"
    description = "Run a short Python snippet in a sandboxed subprocess (off by default)."

    async def call(self, *, code: str, timeout: float = 15.0, **_: Any) -> PluginResult:
        if not _enabled():
            return PluginResult(
                output=None,
                ok=False,
                error=(
                    "code_exec disabled. Set ANTHILL_PLUGIN_CODE_EXEC_ENABLED=1 "
                    "to enable (off by default for safety)."
                ),
            )

        if not code.strip():
            return PluginResult(output=None, ok=False, error="empty code")

        workspace = _workspace_root()
        workspace.mkdir(parents=True, exist_ok=True)

        # Write to a unique file inside the workspace so the snippet has a
        # path to import from and to write artifacts into.
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="anthill_exec_",
            dir=str(workspace),
            delete=False,
        ) as fp:
            fp.write(code)
            script_path = fp.name

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",  # isolated mode: no env-injected paths
                "-u",  # unbuffered output
                script_path,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    # Minimal env so untrusted code can't fish for secrets.
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(workspace),
                    "PYTHONIOENCODING": "utf-8",
                },
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return PluginResult(output=None, ok=False, error=f"timeout after {timeout}s")
        except Exception as e:  # noqa: BLE001
            return PluginResult(output=None, ok=False, error=str(e))
        finally:
            try:
                Path(script_path).unlink()
            except Exception:  # noqa: BLE001
                pass

        out_text = stdout.decode("utf-8", errors="replace")[:8192]
        err_text = stderr.decode("utf-8", errors="replace")[:8192]
        exit_code = proc.returncode if proc.returncode is not None else -1

        return PluginResult(
            output={"stdout": out_text, "stderr": err_text, "exit_code": exit_code},
            metadata={"exit_code": exit_code, "cwd": str(workspace)},
            ok=(exit_code == 0),
        )
