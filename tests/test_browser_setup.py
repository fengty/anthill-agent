"""0.1.56 — tests for the `/setup browser` one-command Playwright bring-up.

Covers:
  - detect_state: chromium-cache probing across platform paths
  - ensure_browser: idempotent-when-ready, full install path, partial
    install (pip done, chromium missing), failure surface
  - Subprocess wiring uses sys.executable + module form so installs
    land in the active venv

All install steps are stubbed via monkeypatch so the tests don't
actually pip-install or download chromium (200MB!) on CI. The
contract we verify is the COMMAND we'd run and how we react to its
exit code, not the install itself.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from anthill.core.browser_setup import (
    BrowserSetupState,
    detect_state,
    ensure_browser,
)


# --- detect_state ---------------------------------------------------------


def test_detect_state_smoke() -> None:
    """At minimum, detect_state returns the dataclass without crashing."""
    s = detect_state()
    assert isinstance(s, BrowserSetupState)
    # `ready` is the AND of both flags — should always be a bool.
    assert isinstance(s.ready, bool)


def test_browser_setup_state_ready_requires_both_flags() -> None:
    assert BrowserSetupState(True, True).ready is True
    assert BrowserSetupState(True, False).ready is False
    assert BrowserSetupState(False, True).ready is False
    assert BrowserSetupState(False, False).ready is False


def test_detect_chromium_install_finds_macos_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Simulate the macOS Library/Caches/ms-playwright/chromium-* layout
    and confirm we detect it. Other platforms use the same iter logic."""
    from anthill.core import browser_setup

    fake_cache = tmp_path / "ms-playwright"
    (fake_cache / "chromium-1223").mkdir(parents=True)

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(fake_cache))
    assert browser_setup._detect_chromium_install() is True


def test_detect_chromium_install_negative(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from anthill.core import browser_setup

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "missing"))
    # Also block the platform-default fallbacks by overriding $HOME so
    # the test doesn't accidentally find the developer's real install.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert browser_setup._detect_chromium_install() is False


# --- ensure_browser idempotency ------------------------------------------


def test_ensure_browser_no_op_when_ready(monkeypatch: Any) -> None:
    """If both flags are already True, we run nothing and report ok."""
    from anthill.core import browser_setup

    monkeypatch.setattr(
        browser_setup,
        "detect_state",
        lambda: BrowserSetupState(True, True),
    )
    captured: list[str] = []
    result = ensure_browser(on_progress=captured.append)
    assert result.ok is True
    assert result.steps_taken == []
    assert "already enabled" in " ".join(captured)


# --- ensure_browser full install path ------------------------------------


def test_ensure_browser_runs_both_steps_when_nothing_installed(
    monkeypatch: Any,
) -> None:
    """Cold-start case: import fails, chromium missing → run both."""
    from anthill.core import browser_setup

    # Simulate: initial detect says "nothing"; after pip install, the
    # state advances to "playwright importable, chromium missing";
    # after the chromium install, "ready".
    states = iter(
        [
            BrowserSetupState(False, False),  # initial probe
            BrowserSetupState(True, False),   # after pip step (mid_state probe)
            BrowserSetupState(True, True),    # after chromium step (final)
        ]
    )
    monkeypatch.setattr(browser_setup, "detect_state", lambda: next(states))

    invocations: list[list[str]] = []

    def fake_run_step(cmd, *, label, on_progress):
        invocations.append(list(cmd))
        return True, ""

    monkeypatch.setattr(browser_setup, "_run_step", fake_run_step)

    result = ensure_browser(on_progress=lambda _: None)
    assert result.ok is True
    assert result.steps_taken == [
        "pip install playwright",
        "playwright install chromium",
    ]
    # First invocation: pip install. Second: playwright install chromium.
    assert invocations[0][:4] == [sys.executable, "-m", "pip", "install"]
    assert "playwright" in invocations[0][4]  # "playwright>=1.40.0"
    assert invocations[1] == [
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
    ]


def test_ensure_browser_skips_pip_when_already_importable(
    monkeypatch: Any,
) -> None:
    """Partial state — playwright present but chromium absent. Only the
    second step runs (no wasted pip install)."""
    from anthill.core import browser_setup

    # ensure_browser calls detect_state at least:
    #   1. initial probe (`before`)
    #   2. mid_state probe (after the pip step is skipped, still needed
    #      because the code re-reads import state before chromium)
    #   3. final probe (`after`)
    states = iter(
        [
            BrowserSetupState(True, False),  # initial
            BrowserSetupState(True, False),  # mid
            BrowserSetupState(True, True),   # after chromium step
        ]
    )
    monkeypatch.setattr(browser_setup, "detect_state", lambda: next(states))

    invocations: list[list[str]] = []

    def fake_run_step(cmd, *, label, on_progress):
        invocations.append(list(cmd))
        return True, ""

    monkeypatch.setattr(browser_setup, "_run_step", fake_run_step)

    result = ensure_browser(on_progress=lambda _: None)
    assert result.ok is True
    assert result.steps_taken == ["playwright install chromium"]
    assert len(invocations) == 1
    assert "playwright" in invocations[0][2]  # the -m target


# --- ensure_browser failure surface --------------------------------------


def test_ensure_browser_pip_failure_reports_and_stops(monkeypatch: Any) -> None:
    """When pip install fails, we don't proceed to chromium and we
    return a useful error message."""
    from anthill.core import browser_setup

    monkeypatch.setattr(
        browser_setup,
        "detect_state",
        lambda: BrowserSetupState(False, False),
    )

    def fake_run_step(cmd, *, label, on_progress):
        return False, "ERROR: No matching distribution"

    monkeypatch.setattr(browser_setup, "_run_step", fake_run_step)

    result = ensure_browser(on_progress=lambda _: None)
    assert result.ok is False
    assert "pip install failed" in (result.error or "")
    assert result.steps_taken == ["pip install playwright"]


def test_ensure_browser_chromium_failure_after_pip_success(
    monkeypatch: Any,
) -> None:
    """pip ok, chromium download fails — error message reflects the
    second step, both steps recorded."""
    from anthill.core import browser_setup

    states = iter(
        [
            BrowserSetupState(False, False),
            BrowserSetupState(True, False),  # mid-state
            BrowserSetupState(True, False),  # final (still no chromium)
        ]
    )
    monkeypatch.setattr(browser_setup, "detect_state", lambda: next(states))

    calls: list[str] = []

    def fake_run_step(cmd, *, label, on_progress):
        calls.append(cmd[2] if len(cmd) > 2 else "")
        if "pip" in label:
            return True, ""
        return False, "network unreachable"

    monkeypatch.setattr(browser_setup, "_run_step", fake_run_step)

    result = ensure_browser(on_progress=lambda _: None)
    assert result.ok is False
    assert "chromium download failed" in (result.error or "")
    assert result.steps_taken == [
        "pip install playwright",
        "playwright install chromium",
    ]


def test_ensure_browser_pip_succeeds_but_import_still_fails(
    monkeypatch: Any,
) -> None:
    """Weird case: pip claims success but the import test still fails —
    usually means the install landed in a different interpreter. We
    surface a hint about restarting the REPL."""
    from anthill.core import browser_setup

    # Initial probe: nothing. Mid-state probe (after pip): still no
    # playwright. The function should bail with a helpful message.
    states = iter(
        [
            BrowserSetupState(False, False),
            BrowserSetupState(False, False),
        ]
    )
    monkeypatch.setattr(browser_setup, "detect_state", lambda: next(states))
    monkeypatch.setattr(
        browser_setup, "_run_step", lambda *a, **kw: (True, "")
    )

    result = ensure_browser(on_progress=lambda _: None)
    assert result.ok is False
    assert "restart" in (result.error or "").lower()


def test_ensure_browser_uses_provided_python_executable(monkeypatch: Any) -> None:
    """Passing python_executable overrides sys.executable in the
    subprocess command — used by callers that want to install into a
    specific venv distinct from the running interpreter."""
    from anthill.core import browser_setup

    states = iter(
        [
            BrowserSetupState(False, False),
            BrowserSetupState(True, False),
            BrowserSetupState(True, True),
        ]
    )
    monkeypatch.setattr(browser_setup, "detect_state", lambda: next(states))

    invocations: list[list[str]] = []

    def fake_run_step(cmd, *, label, on_progress):
        invocations.append(list(cmd))
        return True, ""

    monkeypatch.setattr(browser_setup, "_run_step", fake_run_step)

    custom_py = "/custom/python"
    ensure_browser(on_progress=lambda _: None, python_executable=custom_py)
    for cmd in invocations:
        assert cmd[0] == custom_py
