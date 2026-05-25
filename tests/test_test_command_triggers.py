"""0.2.42 — /test must force agentic_mode so citizens get tools.

Bug from production (user reported "测试场景完全无法触发"):

  - Default state: nation.agentic_mode = False
  - User types /test "..." → citizens run qa_execute
  - But use_loop = False (agentic_mode is False)
  - So citizens get NO tool access (no bash, no browser)
  - Their prompt says "use bash_run / browser_action"
  - They can only NARRATE, never act
  - parse_verdict finds no VERDICT line → tests "errored"
  - User concludes: /test doesn't actually test anything

The fix: /test, /retest, anthill test CLI all force agentic_mode
ON for the duration of the run, restoring whatever the user had
afterward.

These tests don't go through the full async REPL path (too brittle);
they verify the SCOPE contract: when the handler runs, agentic_mode
flips on.
"""

from __future__ import annotations

import asyncio

import pytest

from anthill.core.nation import Nation


# --- the contract: agentic_mode flips inside _run_inner --------------


def test_agentic_mode_default_off() -> None:
    """Sanity: nations start with agentic_mode unset/False (so the
    bug condition still exists if /test forgets to flip)."""
    n = Nation(name="t")
    assert not getattr(n, "agentic_mode", False)


def test_agentic_scope_helper_flips_and_restores() -> None:
    """The _agentic_scope context manager (used by REPL handlers)
    must set the flag on entry and restore on exit, even if the
    inner block raises."""
    from anthill.cli.repl import _agentic_scope

    n = Nation(name="t")
    n.agentic_mode = False  # type: ignore[attr-defined]

    with _agentic_scope(n):
        assert n.agentic_mode is True

    assert n.agentic_mode is False


def test_agentic_scope_restores_on_exception() -> None:
    """Even when the inner block crashes, the flag goes back."""
    from anthill.cli.repl import _agentic_scope

    n = Nation(name="t")
    n.agentic_mode = False  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError):
        with _agentic_scope(n):
            assert n.agentic_mode is True
            raise RuntimeError("boom")

    assert n.agentic_mode is False


def test_agentic_scope_preserves_explicit_on() -> None:
    """If the user already set agentic_mode=True via /agentic, the
    scope shouldn't FORCE it back to False on exit."""
    from anthill.cli.repl import _agentic_scope

    n = Nation(name="t")
    n.agentic_mode = True  # type: ignore[attr-defined]

    with _agentic_scope(n):
        assert n.agentic_mode is True

    # Still True — that's the user's choice.
    assert n.agentic_mode is True


# --- end-to-end via the data-driven CLI path (no LLM) ----------------


def test_cli_run_with_cases_enables_agentic_mode(tmp_path) -> None:
    """The shared `_run_with_cases` (CLI data-driven entry) must set
    agentic_mode=True before kicking off the case loop. We don't run
    the full thing (no LLM); we patch nation.run to capture state
    and verify the flag was set."""
    import anthill.cli.test_cmd as tc
    from anthill.config import AnthillConfig

    seen_modes: list[bool] = []

    class _StubNation:
        name = "stub"
        agentic_mode = False  # initial

        async def run(self, task_type, prompt, **kwargs):
            from anthill.core.agent import TaskResult
            seen_modes.append(getattr(self, "agentic_mode", False))
            return TaskResult(
                task_id="t1", agent_id="ant-x", task_type=task_type,
                output="VERDICT: PASS",
                success_score=1.0, duration_seconds=0.01,
            )

    # Minimal config — just home for path resolution.
    cfg = AnthillConfig(home=tmp_path)
    n = _StubNation()
    from anthill.core.qa import TestCase
    cases = [TestCase(id=1, name="dummy", expected="x")]

    exit_code = asyncio.run(tc._run_with_cases(
        nation=n, config=cfg, requirement="x",
        cases=cases, fix_attempts=0,
        junit_xml=None, report_path=None, quiet=True,
    ))
    # Citizen saw agentic_mode=True before running.
    assert seen_modes == [True], (
        f"expected agentic_mode=True during case run, got {seen_modes}"
    )
    # And it stays True after (CLI doesn't restore — fine for one-shot CLI).
    assert n.agentic_mode is True
    assert exit_code == 0


def test_cli_run_path_enables_agentic_before_case_gen(tmp_path) -> None:
    """The full `_run` (LLM-driven) flips the flag BEFORE calling
    qa_plan, so even the case-generation step happens with tools
    available (in case the planner ever needs to inspect files etc)."""
    import anthill.cli.test_cmd as tc
    from anthill.config import AnthillConfig

    seen_modes: list[tuple[str, bool]] = []

    class _StubNation:
        name = "stub"
        agentic_mode = False

        async def run(self, task_type, prompt, **kwargs):
            from anthill.core.agent import TaskResult
            seen_modes.append((task_type, getattr(self, "agentic_mode", False)))
            if task_type == "qa_plan":
                return TaskResult(
                    task_id="t1", agent_id="ant-x", task_type=task_type,
                    output='{"cases": [{"name": "x", "steps": ["go"], "expected": "ok"}]}',
                    success_score=1.0, duration_seconds=0.01,
                )
            return TaskResult(
                task_id="t2", agent_id="ant-x", task_type=task_type,
                output="VERDICT: PASS",
                success_score=1.0, duration_seconds=0.01,
            )

    cfg = AnthillConfig(home=tmp_path)
    n = _StubNation()
    exit_code = asyncio.run(tc._run(
        nation=n, config=cfg, requirement="check the login page",
        fix_attempts=0, junit_xml=None, report_path=None,
        max_cases=5, quiet=True,
    ))
    # Both qa_plan AND qa_execute observed agentic_mode=True.
    assert seen_modes[0] == ("qa_plan", True), (
        f"qa_plan ran without agentic_mode: {seen_modes}"
    )
    assert all(mode for _, mode in seen_modes), (
        f"some step ran without agentic_mode: {seen_modes}"
    )
    assert exit_code == 0
