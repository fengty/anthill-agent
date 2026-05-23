"""0.2.20 — fast-path shell-intent detection.

When the user types `ping 192.168.1.149` they want output, not a
4.6-second LLM round-trip that suggests they run the command they
already typed. `looks_like_shell_command(text)` returns the cleaned
command for literal shell input, else None.

Tests cover:
  - Positive: known commands with various arg shapes
  - Negative: questions, multi-paragraph prose
  - Wrapper stripping: backticks, $ prefix, ! prefix, code fence
  - `! prefix` opt-in: any command runs (including unknown ones)
  - Sudo passthrough: `sudo apt update` → accepted via `apt`
"""

from __future__ import annotations

import pytest

from anthill.core.shell import looks_like_shell_command


# --- positive cases (should fast-path) ----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "ping 192.168.1.149",
        "ping -c 5 1.1.1.1",
        "git status",
        "git log --oneline -10",
        "df -h",
        "ls -la /tmp",
        "ps aux",
        "curl https://api.example.com/health",
        "docker ps -a",
        "kubectl get pods",
        "npm install",
        "make build",
        "echo hello",
        "find . -name '*.py'",
        "grep -r foo src/",
        "cat /etc/hosts",
        "uname -a",
        "whoami",
    ],
)
def test_known_command_detected(text: str) -> None:
    assert looks_like_shell_command(text) == text


# --- negative cases (must NOT fast-path) --------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Questions about commands
        "ping 通吗？",
        "git status 怎么用",
        "what is df -h",
        "如何使用 curl?",
        "is npm install safe?",
        # Prose
        "I need to ping 192.168.1.149 but my network is flaky",
        "解释一下 df -h 的输出",
        # Multi-line
        "ls\ncat\nls",
        # Empty
        "",
        "   ",
        # Long prose
        "x" * 250,
        # Unknown leading word
        "frobnicate the doohickey",
        "explain mysql group replication",
        # Just text without command structure
        "hello world how are you today",
    ],
)
def test_non_command_rejected(text: str) -> None:
    assert looks_like_shell_command(text) is None


# --- wrapper stripping --------------------------------------------


def test_dollar_prompt_stripped() -> None:
    assert looks_like_shell_command("$ ls -la") == "ls -la"
    assert looks_like_shell_command("$ls -la") == "ls -la"


def test_backtick_wrapper_stripped() -> None:
    assert looks_like_shell_command("`git status`") == "git status"


def test_code_fence_stripped() -> None:
    """Single-line bash fence → just the command."""
    fenced = "```bash\nping 1.1.1.1\n```"
    assert looks_like_shell_command(fenced) == "ping 1.1.1.1"


def test_code_fence_multiline_rejected() -> None:
    """Multiple statements in a fence → don't auto-run (could be
    a tutorial example)."""
    fenced = "```bash\nls\ngit status\n```"
    assert looks_like_shell_command(fenced) is None


# --- ! opt-in escape hatch ---------------------------------------


def test_bang_prefix_allows_unknown_command() -> None:
    """`! my-custom-tool arg` runs even though my-custom-tool isn't
    in KNOWN_COMMANDS. The bang is the user's explicit opt-in."""
    assert looks_like_shell_command("! my-custom-tool arg") == "my-custom-tool arg"
    assert looks_like_shell_command("!my-custom-tool arg") == "my-custom-tool arg"


def test_bang_prefix_allows_complex_pipeline() -> None:
    """`! cat foo | head -5 | wc -l` — user said run it, run it."""
    cmd = "! cat foo | head -5 | wc -l"
    assert looks_like_shell_command(cmd) == "cat foo | head -5 | wc -l"


def test_bang_alone_returns_none() -> None:
    """Lone `!` with no command shouldn't crash or run an empty cmd."""
    assert looks_like_shell_command("!") is None
    assert looks_like_shell_command("! ") is None


# --- sudo passthrough --------------------------------------------


def test_sudo_with_known_command() -> None:
    """`sudo apt update` accepted because `apt` is known."""
    assert looks_like_shell_command("sudo apt update") == "sudo apt update"


def test_sudo_with_unknown_command_rejected() -> None:
    """`sudo frobnicate` not accepted — `frobnicate` isn't known.
    User can force via `! sudo frobnicate`."""
    assert looks_like_shell_command("sudo frobnicate") is None


# --- edge cases --------------------------------------------------


def test_length_cap_at_200_chars() -> None:
    """Long commands are suspicious — probably prose with a verb."""
    long_cmd = "ls " + ("a/" * 100)  # > 200 chars
    assert looks_like_shell_command(long_cmd) is None


def test_question_marker_rejects_known_command() -> None:
    """`ping 通吗?` is a question about ping, not a ping invocation."""
    assert looks_like_shell_command("ping 通吗?") is None


def test_pipe_in_command_accepted() -> None:
    """`grep foo bar | head` should be accepted — first token is grep."""
    assert looks_like_shell_command("grep foo bar | head") == "grep foo bar | head"


def test_redirect_in_command_accepted() -> None:
    """`echo hi > /tmp/x` — first token echo is known."""
    assert looks_like_shell_command("echo hi > /tmp/x") == "echo hi > /tmp/x"
