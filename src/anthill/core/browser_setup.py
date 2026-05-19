"""0.1.56 — one-command Playwright bring-up.

Why this exists: the 0.1.54 browser fallback only fires if Playwright
is importable AND chromium is installed. Both are opt-in
(`anthill-agent[browser]` extra + `playwright install chromium`).
That's two manual steps, two opportunities to give up, and the
documented path didn't match where most users actually live (inside
the REPL, not the shell).

This module ships the **彻底解决** path: a single function that
detects current state, runs whatever's missing, streams progress
to a callback the REPL can render live. The `/setup browser`
REPL command and the `anthill setup browser` CLI command are both
thin wrappers over `ensure_browser()`.

State machine:
  - import playwright → fail → need pip install + chromium download
  - import playwright → ok, chromium not on disk → need download only
  - import playwright → ok, chromium on disk → already good

Idempotent. Safe to re-run. Returns a structured `BrowserSetupResult`
the REPL can print without parsing exit codes.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# A callback the install loop streams each progress line to. The REPL
# wires this to `console.print`; the CLI uses plain `click.echo`.
# Callbacks are sync so the installer itself can stay sync (it's a
# blocking pip / playwright subprocess anyway).
ProgressCallback = Callable[[str], None]


@dataclass
class BrowserSetupState:
    """Snapshot of what's installed and what isn't."""

    playwright_importable: bool
    chromium_installed: bool

    @property
    def ready(self) -> bool:
        return self.playwright_importable and self.chromium_installed


@dataclass
class BrowserSetupResult:
    """End-state report after `ensure_browser()` runs."""

    ok: bool
    state_before: BrowserSetupState
    state_after: BrowserSetupState
    steps_taken: list[str] = field(default_factory=list)
    error: str | None = None


def _detect_chromium_install() -> bool:
    """Has `playwright install chromium` been run?

    Playwright stores downloaded browsers under
    `~/Library/Caches/ms-playwright/` on macOS, `~/.cache/ms-playwright/`
    on Linux, `%USERPROFILE%\\AppData\\Local\\ms-playwright\\` on Windows.
    We don't need to map all three — just check the documented env var
    `PLAYWRIGHT_BROWSERS_PATH` first, then probe each known default.

    Returns True when a `chromium*` directory is present. False is
    a conservative answer that triggers a re-download attempt; the
    `playwright install` step is itself idempotent so a false-positive
    "missing" just wastes a few seconds re-verifying.
    """
    import os

    candidates: list[Path] = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        candidates.append(Path(env_path))

    # Per-platform defaults.
    home = Path.home()
    candidates.extend(
        [
            home / "Library" / "Caches" / "ms-playwright",  # macOS
            home / ".cache" / "ms-playwright",              # Linux
            home / "AppData" / "Local" / "ms-playwright",   # Windows
        ]
    )

    for cache in candidates:
        if not cache.exists():
            continue
        for child in cache.iterdir():
            if child.is_dir() and child.name.startswith("chromium"):
                return True
    return False


def detect_state() -> BrowserSetupState:
    """Quick read-only check. Suitable for the welcome-splash nudge."""
    try:
        import playwright  # noqa: F401 — import only, don't use
        playwright_importable = True
    except ImportError:
        playwright_importable = False
    return BrowserSetupState(
        playwright_importable=playwright_importable,
        chromium_installed=_detect_chromium_install(),
    )


def _run_step(
    cmd: list[str],
    *,
    label: str,
    on_progress: ProgressCallback,
) -> tuple[bool, str]:
    """Run one install sub-command, streaming each output line.

    Returns (ok, last_error_line). We stream the live stdout/stderr
    to `on_progress` so the REPL can show ongoing downloads instead
    of going silent for 30+ seconds.
    """
    on_progress(f"  {label}…")
    try:
        # We deliberately use subprocess.Popen + iter(readline) instead of
        # subprocess.run(capture_output=True) so progress bars from
        # `playwright install` (which writes carriage-returns to stderr)
        # render to the user in real time instead of all at once at the
        # end. Each LINE is fed to the callback — bandwidth is fine for
        # the few hundred lines a typical install emits.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
        )
    except FileNotFoundError as e:
        return False, str(e)

    last_line = ""
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            last_line = line
            # Surface ALL lines for transparency. The download progress
            # bars are noisy but reassuring — going silent for 200MB
            # download is worse UX than a busy stream.
            on_progress(f"    {line}")
    proc.wait()
    if proc.returncode != 0:
        return False, last_line or f"exit {proc.returncode}"
    return True, ""


def ensure_browser(
    *,
    on_progress: ProgressCallback,
    python_executable: str | None = None,
) -> BrowserSetupResult:
    """Make browser fallback work. Idempotent. One-stop install.

    The two install steps run only when needed:
      1. `pip install playwright>=1.40.0` — if import fails
      2. `playwright install chromium` — if no cached chromium dir

    Both run as subprocesses against `python_executable` (defaults to
    the running interpreter) so it always installs into the same venv
    Anthill itself is running from. This sidesteps the "I ran pip
    install but it went to system Python" bug class.

    `on_progress` receives each output line for streaming UI. Empty
    callback works too — the install just runs silently.
    """
    py = python_executable or sys.executable
    before = detect_state()
    if before.ready:
        on_progress("  [green]✓[/green] browser fallback already enabled.")
        return BrowserSetupResult(
            ok=True,
            state_before=before,
            state_after=before,
            steps_taken=[],
        )

    steps: list[str] = []

    # Step 1: pip install if missing.
    if not before.playwright_importable:
        ok, err = _run_step(
            [py, "-m", "pip", "install", "playwright>=1.40.0"],
            label="pip install playwright",
            on_progress=on_progress,
        )
        steps.append("pip install playwright")
        if not ok:
            return BrowserSetupResult(
                ok=False,
                state_before=before,
                state_after=detect_state(),
                steps_taken=steps,
                error=f"pip install failed: {err}",
            )

    # Re-check importability before chromium step — pip install in the
    # same venv normally makes the import work without process restart.
    mid_state = detect_state()
    if not mid_state.playwright_importable:
        return BrowserSetupResult(
            ok=False,
            state_before=before,
            state_after=mid_state,
            steps_taken=steps,
            error=(
                "pip succeeded but `import playwright` still fails. "
                "Restart the REPL and try again — Python's import "
                "cache picks up the new package on next start."
            ),
        )

    # Step 2: chromium binary download if missing.
    if not mid_state.chromium_installed:
        ok, err = _run_step(
            [py, "-m", "playwright", "install", "chromium"],
            label="downloading chromium (~200MB)",
            on_progress=on_progress,
        )
        steps.append("playwright install chromium")
        if not ok:
            return BrowserSetupResult(
                ok=False,
                state_before=before,
                state_after=detect_state(),
                steps_taken=steps,
                error=f"chromium download failed: {err}",
            )

    after = detect_state()
    return BrowserSetupResult(
        ok=after.ready,
        state_before=before,
        state_after=after,
        steps_taken=steps,
    )
