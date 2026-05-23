"""0.2.25 — fast-path runs interp only when worth it.

The fast path's whole point is no-LLM. But on failure (or huge
output), the user usually wants help reading the output. We
trigger ONE interp call in those specific cases.

Contract:
  - exit 0 + short stdout → no interp (raw output is enough)
  - exit != 0 → interp
  - timeout → interp
  - exit 0 + huge stdout from non-listing command (e.g. pytest with
    long traceback) → interp
  - exit 0 + huge stdout from `ls` / `find` / `grep` → NO interp
    (lists are self-explanatory)
  - blocked → no interp (refusal message already explains)

We also verify test-runner CLIs (pytest, jest, tox, etc.) are
in KNOWN_COMMANDS so they fast-path.
"""

from __future__ import annotations

from anthill.core.shell import (
    ShellResult,
    looks_like_shell_command,
    should_interpret_fast_path,
)


def _result(
    *,
    cmd: str = "echo hi",
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    blocked_reason: str | None = None,
) -> ShellResult:
    return ShellResult(
        command=cmd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        timed_out=timed_out,
        blocked_reason=blocked_reason,
    )


# --- when to interp ---------------------------------------------------


def test_no_interp_on_clean_success() -> None:
    """`echo hi` → exit 0, short output. No need to spend an LLM
    call telling the user 'echo printed hi'."""
    r = _result(cmd="echo hi", returncode=0, stdout="hi\n")
    assert should_interpret_fast_path(r) is False


def test_interp_on_failure() -> None:
    """Non-zero exit → interpret. This is the test-runner case."""
    r = _result(
        cmd="pytest -x",
        returncode=1,
        stdout="FAILED tests/foo.py::test_x — AssertionError",
        stderr="",
    )
    assert should_interpret_fast_path(r) is True


def test_interp_on_timeout() -> None:
    """Timeout is failure-shaped. Interpret."""
    r = _result(cmd="sleep 100", returncode=-1, timed_out=True)
    assert should_interpret_fast_path(r) is True


def test_interp_on_huge_non_list_output() -> None:
    """A long non-list output (e.g. pytest passing 200 tests with
    verbose output, or curl returning a big JSON) → summarize."""
    long_stdout = "test_x passed\n" * 60  # > 40 lines
    r = _result(cmd="pytest -v", returncode=0, stdout=long_stdout)
    assert should_interpret_fast_path(r) is True


def test_no_interp_on_huge_list_output() -> None:
    """`find /` / `ls -R` — lists are inherently long. Don't try
    to summarize 'here are 1000 file paths' into 1 sentence."""
    long_listing = "/path/to/file\n" * 500
    r = _result(cmd="find /tmp", returncode=0, stdout=long_listing)
    assert should_interpret_fast_path(r) is False

    r = _result(cmd="ls -R /", returncode=0, stdout=long_listing)
    assert should_interpret_fast_path(r) is False


def test_no_interp_when_blocked() -> None:
    """A refused command already has a clear refusal message;
    don't add another layer."""
    r = _result(cmd="rm -rf /", blocked_reason="hard-deny matched")
    assert should_interpret_fast_path(r) is False


# --- test runners are fast-pathable ----------------------------------


def test_pytest_is_known_command() -> None:
    """0.2.25 added pytest to KNOWN_COMMANDS so the user can just
    type `pytest tests/` and have it fast-path."""
    assert looks_like_shell_command("pytest tests/") == "pytest tests/"
    assert looks_like_shell_command("pytest -x --tb=short") == "pytest -x --tb=short"


def test_jest_and_tox_known() -> None:
    """Other common test runners are also recognized."""
    assert looks_like_shell_command("jest --watch") == "jest --watch"
    assert looks_like_shell_command("tox -e py39") == "tox -e py39"
    assert looks_like_shell_command("vitest run") == "vitest run"
    assert looks_like_shell_command("playwright test") == "playwright test"
