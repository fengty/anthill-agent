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


# --- positive / negative discrimination -----------------------------


def test_known_commands_accepted() -> None:
    """Representative commands across categories — net/git/file/build/exec.
    One assertion per category. If KNOWN_COMMANDS regresses, at least
    one category fails and we know roughly where."""
    accepts = [
        "ping 192.168.1.149",              # network
        "git log --oneline -10",            # vcs
        "df -h",                            # filesystem
        "docker ps -a",                     # container
        "npm install",                      # package mgr
        "grep -r foo src/",                 # text proc
    ]
    for text in accepts:
        assert looks_like_shell_command(text) == text, (
            f"should fast-path: {text!r}"
        )


def test_non_commands_rejected() -> None:
    """Discrimination test — these are the SHAPES of input that
    must NOT fast-path. Each kind shown once; if a new false
    positive shows up, add a case here, don't paper it over."""
    rejects = [
        "ping 通吗？",                       # question particle (Chinese)
        "what is df -h",                     # question particle (English)
        "I need to ping 192.168.1.149 but my network is flaky",  # prose
        "ls\ncat\nls",                       # multi-line
        "",                                  # empty
        "frobnicate the doohickey",          # unknown command
        "x" * 250,                           # too long
    ]
    for text in rejects:
        assert looks_like_shell_command(text) is None, (
            f"should NOT fast-path: {text!r}"
        )


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
