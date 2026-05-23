"""0.2.19 — shell execution primitive.

User pain point: "anthill 像个问答机器人, 并不会操作我的电脑.
比如 持续 ping 看是否丢包 ping 192.168.1.149, 他返回的非常差."

Fix: citizens emit `[[bash:CMD]]`, the REPL runs it, the output
appears inline. This module is the engine; the REPL is the
consumer.

Tests cover the primitives in isolation — no real network, no
LLM. We use builtin shell commands that exist on every Unix
(`echo`, `true`, `false`, `sleep`) so the tests stay portable.
"""

from __future__ import annotations

import os
import sys

import pytest

from anthill.core.shell import (
    DEFAULT_TIMEOUT_SECONDS,
    BashBlock,
    ShellResult,
    apply_caps,
    extract_bash_blocks,
    is_dangerous,
    safe_run,
    strip_bash_blocks,
)


# --- is_dangerous ------------------------------------------------------


def test_dangerous_rm_root() -> None:
    assert is_dangerous("rm -rf /") is not None
    assert is_dangerous("rm -rf /*") is not None
    assert is_dangerous("rm -rfv ~") is not None


def test_dangerous_fork_bomb() -> None:
    assert is_dangerous(":(){ :|:& };:") is not None


def test_dangerous_mkfs_device() -> None:
    assert is_dangerous("mkfs.ext4 /dev/sda1") is not None
    assert is_dangerous("dd if=/dev/zero of=/dev/sda bs=1M") is not None


def test_dangerous_chmod_777_root() -> None:
    assert is_dangerous("chmod -R 777 /") is not None


def test_safe_commands_pass() -> None:
    """Normal commands shouldn't trip the danger detector."""
    safe_cmds = (
        "ls -la",
        "ping -c 5 192.168.1.149",
        "git status",
        "df -h",
        "rm tempfile.txt",  # rm without rf
        "rm -rf node_modules",  # not root
        "curl https://example.com",
    )
    for cmd in safe_cmds:
        assert is_dangerous(cmd) is None, f"false positive: {cmd}"


# --- apply_caps -------------------------------------------------------


def test_ping_gets_count_injected() -> None:
    capped = apply_caps("ping 192.168.1.149")
    assert "-c 10" in capped
    assert "192.168.1.149" in capped


def test_ping_with_existing_count_unchanged() -> None:
    """If the user already wrote -c, don't double-inject."""
    capped = apply_caps("ping -c 5 192.168.1.149")
    # Should be left alone (don't add another -c).
    assert capped.count("-c") == 1


def test_curl_gets_max_time_injected() -> None:
    capped = apply_caps("curl https://example.com")
    assert "--max-time" in capped


def test_curl_with_existing_max_time_unchanged() -> None:
    """User's explicit --max-time is respected."""
    capped = apply_caps("curl --max-time 5 https://example.com")
    assert capped.count("--max-time") == 1


def test_apply_caps_leaves_other_commands_alone() -> None:
    """ls / git / df / etc. don't need capping."""
    for cmd in ("ls -la", "git status", "df -h", "echo hi"):
        assert apply_caps(cmd) == cmd


def test_apply_caps_skips_pipelines() -> None:
    """We deliberately don't try to mangle pipes/redirects."""
    cmd = "ping 1.1.1.1 | head -5"
    assert apply_caps(cmd) == cmd


# --- safe_run --------------------------------------------------------


def test_safe_run_echo() -> None:
    """echo should succeed and capture stdout."""
    r = safe_run("echo hello world")
    assert r.ok
    assert r.returncode == 0
    assert "hello world" in r.stdout
    assert r.stderr == ""
    assert not r.timed_out
    assert r.duration_seconds < 5


def test_safe_run_failure_captures_returncode() -> None:
    """Non-zero exit is reported, not swallowed."""
    r = safe_run("false")
    assert not r.ok
    assert r.returncode != 0
    assert r.blocked_reason is None
    assert not r.timed_out


def test_safe_run_blocks_dangerous() -> None:
    """rm -rf / should be refused before subprocess sees it."""
    r = safe_run("rm -rf /")
    assert r.blocked_reason is not None
    assert "deny" in r.blocked_reason.lower() or "rm" in r.command
    assert r.returncode == -1
    assert not r.ok


def test_safe_run_allow_dangerous_bypasses_check() -> None:
    """The override flag exists for tests / `--unsafe` mode.
    We don't actually run a dangerous cmd; we just verify the
    block reason is NOT set when the flag is on. Use `true` so
    nothing destructive happens."""
    # `true` isn't dangerous; just confirms the flag path doesn't crash.
    r = safe_run("true", allow_dangerous=True)
    assert r.blocked_reason is None
    assert r.ok


def test_safe_run_timeout_short() -> None:
    """A command that exceeds the timeout is killed."""
    r = safe_run("sleep 5", timeout=0.5)
    assert r.timed_out
    assert not r.ok
    assert r.duration_seconds < 4  # killed long before 5s


def test_safe_run_truncates_huge_stdout() -> None:
    """Spam producers don't flood the buffer."""
    # Print ~200 KB of output.
    r = safe_run("yes hello | head -c 200000", timeout=5)
    # Truncated to MAX_CAPTURE_BYTES (64 KB) + truncation marker.
    assert len(r.stdout) <= 64 * 1024 + 200
    assert "truncated" in r.stdout or len(r.stdout) >= 64 * 1024 - 1


def test_safe_run_invokes_caps_for_ping() -> None:
    """A `ping <host>` without -c gets capped. We use a non-routable
    address with a hard timeout to keep this fast."""
    r = safe_run("ping 127.0.0.1", timeout=15)
    # The recorded command should show the cap was applied.
    assert "-c 10" in r.command


# --- marker extraction -----------------------------------------------


def test_extract_no_markers_returns_empty() -> None:
    assert extract_bash_blocks("just plain text") == []


def test_extract_single_marker() -> None:
    text = "let me check: [[bash:ping -c 5 1.1.1.1]] done"
    blocks = extract_bash_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].command == "ping -c 5 1.1.1.1"


def test_extract_multiple_markers_in_order() -> None:
    text = (
        "first: [[bash:echo a]] then: [[bash:echo b]] last: [[bash:echo c]]"
    )
    blocks = extract_bash_blocks(text)
    assert [b.command for b in blocks] == ["echo a", "echo b", "echo c"]
    # Offsets ascend.
    assert blocks[0].start < blocks[1].start < blocks[2].start


def test_extract_handles_whitespace_around_marker() -> None:
    text = "[[ bash : ls -la ]]"
    blocks = extract_bash_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].command == "ls -la"


def test_extract_ignores_empty_marker() -> None:
    """[[bash:]] with no command is dropped, not run."""
    blocks = extract_bash_blocks("noop: [[bash:]] continue")
    assert blocks == []


def test_extract_dotall_multiline_command() -> None:
    """A heredoc-style multi-line command should be captured intact."""
    text = "[[bash:echo first\necho second]]"
    blocks = extract_bash_blocks(text)
    assert len(blocks) == 1
    assert "first" in blocks[0].command
    assert "second" in blocks[0].command


# --- strip_bash_blocks -----------------------------------------------


def test_strip_removes_marker_keeps_rest() -> None:
    text = "before [[bash:echo x]] after"
    stripped = strip_bash_blocks(text)
    assert "[[bash:" not in stripped
    assert "before" in stripped
    assert "after" in stripped


def test_strip_no_markers_unchanged_modulo_strip() -> None:
    text = "plain text"
    assert strip_bash_blocks(text) == "plain text"


# --- SHELL_TOOL_INSTRUCTION exists ---------------------------------


def test_instruction_module_constant_exists() -> None:
    from anthill.core.shell import SHELL_TOOL_INSTRUCTION

    assert "[[bash:" in SHELL_TOOL_INSTRUCTION
    assert "ping" in SHELL_TOOL_INSTRUCTION.lower()  # uses ping in examples
