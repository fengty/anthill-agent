"""0.2.21 — Real-world failure mode tests.

These tests capture the SHAPE of bugs the user actually hit in
production. Adding a test here means: "I saw this fail in real
use. Don't let it regress silently."

Every test in this file should:
  1. Reference a specific session/turn where the bug appeared
  2. Exercise an end-to-end path (not an isolated unit)
  3. Be valuable AS A TEST — failing here means a real UX bug

Existing real-scenarios captured:

  - 0.2.19/0.2.20: "ping 192.168.1.149" went through Scout/LLM
    and produced a `​​​```bash` code fence + "想展开告诉我"
    instead of running. Cost 4.6s and 362 tokens for nothing.
    → looks_like_shell_command fast-path

  - 0.2.18: /loop dies after iter 3 even though earlier iterations
    emitted markers correctly. brevity/loop-marker conflict.
    → consecutive-miss tracking + brevity suppression in loops

  - 0.2.8: "你如何和我的飞书对接的？" after a mysql conversation
    inherits the mysql wrap → clarify asks "are you asking about
    mysql or feishu integration?"
    → self-context detection + conversation wrap skip
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


# --- the "ping 192.168.1.149" bug -------------------------------------


def test_literal_command_takes_fast_path() -> None:
    """User types `ping 192.168.1.149`. anthill should detect this
    is a literal shell command and execute directly — NOT route to
    Scout/LLM that suggests they run the very command they typed.

    This was burning 4.6s and 362 tokens per ping in production.
    """
    from anthill.core.shell import looks_like_shell_command

    # Pure command — fast-path.
    assert looks_like_shell_command("ping 192.168.1.149") == "ping 192.168.1.149"
    # Pure command — fast-path.
    assert looks_like_shell_command("git status") == "git status"
    # Question — NO fast-path (LLM should explain).
    assert looks_like_shell_command("ping 通吗？") is None
    # Mixed natural language + command — NO fast-path (ambiguous,
    # let LLM extract the intent via [[bash:]]).
    mixed = "持续 ping 看是否丢包 ping 192.168.1.149"
    assert looks_like_shell_command(mixed) is None


def test_ping_actually_runs_and_caps() -> None:
    """End-to-end: typing `ping 127.0.0.1` runs ping, auto-caps to
    -c 10, returns within seconds with 0% loss for loopback."""
    from anthill.core.shell import safe_run

    r = safe_run("ping 127.0.0.1", timeout=15)
    # The fast-path injected -c 10.
    assert "-c 10" in r.command
    # Loopback always succeeds.
    assert r.ok, f"ping 127.0.0.1 failed: {r.stderr!r}"
    # And actually executed (no truncation needed at 10 packets).
    assert "127.0.0.1" in r.stdout
    # Bounded execution: 10 packets at 1Hz = ~10s, well under timeout.
    assert r.duration_seconds < 13


# --- the "/loop dies on one missed marker" bug ------------------------


def test_loop_survives_intermittent_marker_misses() -> None:
    """0.2.18 regression: the loop used to die when the model
    forgot the marker at iter 3+. Real session: iter 1/2 emitted
    `[[loop:continue]]`, iter 3 emitted prose without marker →
    loop killed despite the work being mid-flight.

    Now: only consecutive misses count, so a one-off slip survives.
    """
    import anthill.core.loop as loop_mod
    from anthill.core.loop import LoopSpec, LoopState, run_loop

    # Real-shape iterations: marker, marker, MISS, marker, marker+done.
    outputs = [
        "tick 1 [[loop:continue]]",
        "tick 2 [[loop:continue]]",
        "tick 3 forgot marker",      # miss
        "tick 4 [[loop:continue]]",  # reset
        "tick 5 [[loop:done]]",
    ]
    async def iter_fn(state: LoopState) -> str:
        return outputs[state.iteration - 1]

    # Patch the post-miss sleep to keep the test fast.
    saved = loop_mod._NO_MARKER_DEFAULT_WAIT_SECONDS
    loop_mod._NO_MARKER_DEFAULT_WAIT_SECONDS = 0.0
    try:
        spec = LoopSpec(
            interval_seconds=0.0,
            request="x",
            self_paced=True,
            max_iterations=10,
        )
        state = asyncio.run(run_loop(spec, on_iteration=iter_fn))
    finally:
        loop_mod._NO_MARKER_DEFAULT_WAIT_SECONDS = saved

    # Critical: didn't die on iter 3.
    assert state.iteration == 5, (
        f"loop died early at iter {state.iteration} (pre-0.2.18 bug)"
    )
    assert state.stop_reason == "model_done"


# --- the "self-referential ask wrapped in prior conversation" bug -----


def test_self_referential_ask_after_unrelated_topic() -> None:
    """0.2.8 regression: after a mysql conversation, asking
    'how do you connect to lark?' was being wrapped with mysql
    context → clarify asked 'are you asking about mysql OR lark?'

    Now: looks_self_referential fires, and the REPL drops the
    wrap. Validate the contract that drives that behavior."""
    from anthill.core.conversation import ConversationContext, is_follow_up
    from anthill.core.self_context import looks_self_referential

    c = ConversationContext()
    c.record("帮我部署 mysql 中间件", "用 RDS for MySQL ...")

    new_ask = "你如何和我的飞书对接的？"
    # Without the self-context detection, this would just be a
    # follow-up to mysql.
    assert is_follow_up(new_ask, c.last_turn()) is True
    # The self-context guard fires, telling the REPL to drop the
    # conversation wrap.
    assert looks_self_referential(new_ask) is True


# --- the "shell exec disabled" bug ------------------------------------


def test_noexec_actually_suppresses_marker_in_prompt() -> None:
    """User runs /noexec because they want to review what citizens
    suggest before anything runs. The system prompt must drop the
    shell tool contract so citizens stop emitting [[bash:]]."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation

    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    # Default: contract present.
    assert "[[bash:" in (n._compose_system(n.agents[0]) or "")
    # /noexec on: contract dropped.
    n._exec_disabled = True  # type: ignore[attr-defined]
    assert "[[bash:" not in (n._compose_system(n.agents[0]) or "")


# --- the "model emits markdown ```bash``` instead of [[bash:]]" path --


def test_markdown_bash_fence_does_not_auto_execute() -> None:
    """When citizen output contains a markdown code fence (NOT a
    [[bash:]] marker), the REPL must NOT execute it. That fence
    might be example code the model is explaining."""
    from anthill.core.shell import extract_bash_blocks

    # ```bash + cmd + ``` → NO marker extraction.
    text = "Here's how:\n```bash\nrm -rf /\n```\n"
    blocks = extract_bash_blocks(text)
    assert blocks == [], (
        "markdown fence must NOT be treated as executable"
    )


# --- the "/retry burns same citizen" bug ----------------------------


def test_retry_actually_forbids_prior_citizens() -> None:
    """The /retry command's whole value: different citizens try.
    If the executor doesn't honor initial_forbid, /retry is just
    'run it again' which has no learning value."""
    import asyncio as _asyncio
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.executor import execute_plan
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan, Subtask

    class _FakeAgent(Agent):
        async def execute(self, task_type, prompt, *, system=None, on_token=None, **kwargs):  # type: ignore[override]
            import uuid
            return TaskResult(
                task_id=f"t-{uuid.uuid4().hex[:6]}",
                agent_id=self.id,
                task_type=task_type,
                output=f"[{self.id}]",
                success_score=1.0,
                duration_seconds=0.01,
            )

    n = Nation(name="t")
    n.agents = [
        _FakeAgent(id="ant-A", model="deepseek"),
        _FakeAgent(id="ant-B", model="minimax"),
    ]
    # ant-A has the stronger trail.
    for _ in range(8):
        n.pheromones.deposit("ant-A", "research", success_score=1.0)
    plan = Plan(subtasks=[Subtask("research", "x", [])])

    outcomes = _asyncio.run(
        execute_plan(plan, n, initial_forbid={"ant-A"})
    )
    assert outcomes[0].final is not None
    # The retry MUST land on ant-B, not ant-A.
    assert outcomes[0].final.agent_id == "ant-B", (
        "/retry didn't forbid the prior citizen"
    )
