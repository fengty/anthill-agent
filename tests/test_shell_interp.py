"""0.2.24 — model sees shell output and gives a brief interpretation.

The piece that was missing: the citizen wrote prose AROUND
[[bash:]] markers before seeing the output. So "0% loss means
reachable" was prediction, not analysis. If the ping actually
returned 100% loss, that prose was misleading.

This module's contract: take (user_question, [(cmd, ShellResult)])
and produce a prompt that the model can answer with a real
1-2 sentence interpretation.

We test the prompt-builder, not the LLM call itself — the call is
plumbed through nation.run in repl.py and would need full LLM
mocking to exercise end-to-end.
"""

from __future__ import annotations

from anthill.core.shell import ShellResult, build_interpretation_prompt


def _ok_result(cmd: str, stdout: str, returncode: int = 0) -> ShellResult:
    return ShellResult(
        command=cmd,
        returncode=returncode,
        stdout=stdout,
        stderr="",
        duration_seconds=0.05,
        timed_out=False,
    )


def test_prompt_includes_user_question_and_cmd_output() -> None:
    """The interpretation prompt must carry: the user's original
    question + the actual command + its real stdout. Without ALL
    THREE, the model can't answer 'what does this mean FOR THE
    USER'S ASK.'"""
    runs = [(
        "ping -c 5 google.com",
        _ok_result(
            "ping -c 5 google.com",
            "5 packets transmitted, 5 received, 0.0% packet loss\n"
            "rtt min/avg/max = 10/12/15 ms",
        ),
    )]
    prompt = build_interpretation_prompt(
        "is google reachable?", runs,
    )
    assert "is google reachable?" in prompt
    assert "ping -c 5 google.com" in prompt
    assert "0.0% packet loss" in prompt


def test_prompt_includes_stderr_and_returncode_on_failure() -> None:
    """When a command FAILED, the interp prompt needs to surface
    the stderr and the non-zero exit so the model can say what
    actually went wrong, not gloss over it."""
    runs = [(
        "curl https://bad.example.com",
        ShellResult(
            command="curl --max-time 20 https://bad.example.com",
            returncode=6,
            stdout="",
            stderr="curl: (6) Could not resolve host: bad.example.com",
            duration_seconds=0.5,
            timed_out=False,
        ),
    )]
    prompt = build_interpretation_prompt("check the api", runs)
    assert "Could not resolve host" in prompt
    assert "exit 6" in prompt


def test_prompt_marks_timeouts_distinctly() -> None:
    """A timeout is different from exit-non-zero. The interp model
    needs to know — same exit code semantics but very different
    user advice ('host is slow' vs 'cmd is broken')."""
    runs = [(
        "ping unreachable.example",
        ShellResult(
            command="ping -c 10 unreachable.example",
            returncode=-1,
            stdout="PING unreachable.example: ...",
            stderr="",
            duration_seconds=30.0,
            timed_out=True,
        ),
    )]
    prompt = build_interpretation_prompt("check it", runs)
    assert "TIMED OUT" in prompt


def test_prompt_handles_multiple_commands_in_order() -> None:
    """When the citizen ran 2+ [[bash:]] blocks, the interp model
    should see them in execution order so the narrative makes
    sense ('first we did A which showed X, then B which showed Y')."""
    runs = [
        ("git branch --show-current", _ok_result(
            "git branch --show-current", "feature/auth\n"
        )),
        ("git status", _ok_result(
            "git status", "On branch feature/auth\nnothing to commit"
        )),
    ]
    prompt = build_interpretation_prompt("am I clean to push?", runs)
    # Both commands present, in order.
    branch_pos = prompt.index("git branch")
    status_pos = prompt.index("git status")
    assert branch_pos < status_pos


def test_prompt_caps_huge_stdout_for_budget() -> None:
    """A `find /` style output shouldn't blow the interp prompt
    budget. We cap each command's stdout in the prompt itself."""
    huge_stdout = "line\n" * 5000  # ~25KB
    runs = [("find /", _ok_result("find /", huge_stdout))]
    prompt = build_interpretation_prompt("look at this", runs)
    # The prompt is bounded — definitely smaller than the raw 25KB.
    assert len(prompt) < 5000


def test_prompt_asks_for_short_plain_answer() -> None:
    """The directive: don't ask for a structured report. We want
    1-2 sentences of practical answer."""
    runs = [("ls", _ok_result("ls", "a\nb\nc"))]
    prompt = build_interpretation_prompt("what's here?", runs)
    # Some signal that brevity is required.
    p = prompt.lower()
    assert "1-2 sentence" in p or "brief" in p or "plain prose" in p


def test_print_final_output_returns_runs() -> None:
    """End-to-end: rendering a model output with a [[bash:]] block
    returns the (cmd, result) tuple list that the caller can feed
    to the interpretation step."""
    from io import StringIO

    from rich.console import Console

    import anthill.cli.repl as repl_mod

    sample = "let me check: [[bash:echo hello]] done"
    buf = StringIO()
    fake = Console(file=buf, force_terminal=False, width=120)
    orig = repl_mod.console
    repl_mod.console = fake
    try:
        runs = repl_mod._print_final_output(sample, exec_enabled=True)
    finally:
        repl_mod.console = orig

    assert len(runs) == 1
    cmd, result = runs[0]
    assert cmd == "echo hello"
    assert "hello" in result.stdout


def test_print_final_output_returns_empty_when_no_markers() -> None:
    """Plain text response → no runs to interpret."""
    import anthill.cli.repl as repl_mod
    from io import StringIO
    from rich.console import Console

    buf = StringIO()
    fake = Console(file=buf, force_terminal=False, width=120)
    orig = repl_mod.console
    repl_mod.console = fake
    try:
        runs = repl_mod._print_final_output("just prose, no markers")
    finally:
        repl_mod.console = orig
    assert runs == []
