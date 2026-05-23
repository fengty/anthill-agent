"""0.2.19 — shell tool integrates into citizen system prompts.

Verifies the contract:
  - By default every citizen's system prompt includes SHELL_TOOL_INSTRUCTION
    so they know they can emit [[bash:CMD]] for action requests
  - When `nation._exec_disabled = True`, the instruction is dropped
    so /noexec actually suppresses the marker (model doesn't learn
    a useless contract)
  - The instruction co-exists with brevity + loop instructions
    without contradicting them
"""

from __future__ import annotations

from anthill.core.agent import Agent
from anthill.core.nation import Nation


def _bare_nation() -> Nation:
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    return n


def test_shell_instruction_in_system_prompt_by_default() -> None:
    """Default state: citizens see the [[bash:]] contract."""
    n = _bare_nation()
    sys_prompt = n._compose_system(n.agents[0]) or ""
    assert "[[bash:" in sys_prompt
    assert "SHELL TOOL" in sys_prompt


def test_shell_instruction_dropped_when_disabled() -> None:
    """/noexec → no marker contract in the system prompt."""
    n = _bare_nation()
    n._exec_disabled = True  # type: ignore[attr-defined]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    assert "[[bash:" not in sys_prompt


def test_shell_instruction_coexists_with_brevity_outside_loop() -> None:
    """Both apply: brevity gives length default, shell gives action contract."""
    n = _bare_nation()
    sys_prompt = n._compose_system(n.agents[0]) or ""
    # Brevity directive marker.
    assert "concise" in sys_prompt.lower() or "under 800" in sys_prompt
    # Shell instruction.
    assert "[[bash:" in sys_prompt


def test_shell_instruction_coexists_with_loop_marker() -> None:
    """In a loop iteration: brevity suppressed, BUT shell + loop both apply.
    The model needs both to (a) do work via [[bash:]] and (b) report
    cadence via [[loop:]]."""
    n = _bare_nation()
    n._in_loop_iteration = True  # type: ignore[attr-defined]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    # Loop instruction is there.
    assert "[[loop:" in sys_prompt
    # Shell instruction is there.
    assert "[[bash:" in sys_prompt
    # Brevity is NOT (per 0.2.18).
    assert "under 800" not in sys_prompt
    n._in_loop_iteration = False  # type: ignore[attr-defined]


def test_no_attribute_error_when_flag_never_set() -> None:
    """A nation that's never seen the flag still composes a sane prompt."""
    n = _bare_nation()
    # Don't set _exec_disabled at all.
    sys_prompt = n._compose_system(n.agents[0]) or ""
    # Default = exec enabled.
    assert "[[bash:" in sys_prompt
