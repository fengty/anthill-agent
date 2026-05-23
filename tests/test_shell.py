"""0.2.19 — shell execution primitive.

User pain point: "anthill 像个问答机器人, 并不会操作我的电脑."

Tests focus on real safety + behavior contracts:
  - dangerous patterns refuse (catastrophic-by-mistake commands)
  - sane caps inject (ping/curl don't tie the REPL down)
  - subprocess timeout / truncation actually work
  - markers parse and strip correctly
"""

from __future__ import annotations

import pytest

from anthill.core.shell import (
    apply_caps,
    extract_bash_blocks,
    is_dangerous,
    safe_run,
    strip_bash_blocks,
)


# --- safety: hard-deny list -------------------------------------------


def test_dangerous_patterns_refused() -> None:
    """Each of these is unambiguous footgun. If the regex stops
    matching any of them, that's a real safety regression."""
    dangerous = (
        "rm -rf /",
        "rm -rf /*",
        "rm -rfv ~",
        ":(){ :|:& };:",            # fork bomb
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda bs=1M",
        "chmod -R 777 /",
    )
    for cmd in dangerous:
        assert is_dangerous(cmd), f"missed: {cmd!r}"


def test_safe_commands_not_refused() -> None:
    """`rm foo.txt` and `rm -rf node_modules` are normal. Don't
    block legitimate work."""
    safe = (
        "ls -la",
        "ping -c 5 192.168.1.149",
        "git status",
        "rm tempfile.txt",
        "rm -rf node_modules",
        "curl https://example.com",
    )
    for cmd in safe:
        assert is_dangerous(cmd) is None, f"false positive: {cmd!r}"


# --- caps: bounded execution by default ------------------------------


def test_caps_inject_for_unbounded_commands() -> None:
    """`ping` without -c blocks forever; `curl` without --max-time
    blocks on slow hosts. These two are the common offenders we
    autocap. User's explicit flags are respected."""
    # ping: cap injected when missing, respected when present.
    assert "-c 10" in apply_caps("ping 192.168.1.149")
    assert apply_caps("ping -c 5 192.168.1.149").count("-c") == 1
    # curl: same.
    assert "--max-time" in apply_caps("curl https://example.com")
    assert apply_caps("curl --max-time 5 X").count("--max-time") == 1


def test_caps_leave_other_commands_alone() -> None:
    """Don't paper over commands that don't need it. Also don't
    mangle pipelines (let the shell handle them)."""
    for cmd in ("ls -la", "git status", "echo hi", "ping 1.1.1.1 | head -5"):
        assert apply_caps(cmd) == cmd


# --- subprocess execution --------------------------------------------


def test_run_captures_stdout_and_exit() -> None:
    """Happy path: echo succeeds, output captured."""
    r = safe_run("echo hello world")
    assert r.ok
    assert "hello world" in r.stdout
    assert r.returncode == 0


def test_run_reports_failure_not_swallows() -> None:
    """Non-zero exit is surfaced, not pretended-okay."""
    r = safe_run("false")
    assert not r.ok
    assert r.returncode != 0


def test_run_blocks_dangerous_before_subprocess() -> None:
    """rm -rf / never reaches subprocess.run."""
    r = safe_run("rm -rf /")
    assert r.blocked_reason is not None
    assert not r.ok


def test_run_kills_on_timeout() -> None:
    """A runaway command is killed at the deadline, not allowed to
    block the REPL."""
    r = safe_run("sleep 5", timeout=0.5)
    assert r.timed_out
    assert r.duration_seconds < 4


def test_run_truncates_huge_output() -> None:
    """A 200 KB output should be capped at MAX_CAPTURE_BYTES (64 KB)."""
    r = safe_run("yes hello | head -c 200000", timeout=5)
    assert len(r.stdout) <= 64 * 1024 + 200
    assert "truncated" in r.stdout


# --- marker parse / strip -------------------------------------------


def test_markers_extract_in_order() -> None:
    """Multiple markers parse, in source order, into trimmed commands.
    Whitespace inside the brackets is tolerated. Empty markers
    drop out."""
    text = (
        "narrating [[bash:echo a]] then "
        "[[ bash : echo b ]] also "
        "ignore: [[bash:]] last: [[bash:echo c]]"
    )
    blocks = extract_bash_blocks(text)
    assert [b.command for b in blocks] == ["echo a", "echo b", "echo c"]
    # Source-order: offsets ascend.
    assert blocks[0].start < blocks[1].start < blocks[2].start


def test_strip_removes_markers_keeps_prose() -> None:
    """Used when /noexec is on — strip markers, keep narration."""
    text = "before [[bash:echo x]] middle [[bash:ls]] after"
    stripped = strip_bash_blocks(text)
    assert "[[bash:" not in stripped
    assert "before" in stripped and "middle" in stripped and "after" in stripped
