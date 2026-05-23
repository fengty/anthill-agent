"""0.2.19 — shell tool integrates into citizen system prompts.

Two contracts worth a regression test:

  1. By default, the shell tool contract is in the system prompt
     (citizens know they CAN emit [[bash:...]])
  2. `/noexec` (sets `nation._exec_disabled = True`) actually
     suppresses the contract

We test the BEHAVIOR — does the contract presence flip with the
flag? — not the exact wording. If we rephrase the instruction the
tests should still pass.
"""

from __future__ import annotations

from anthill.core.agent import Agent
from anthill.core.nation import Nation


def _bare_nation() -> Nation:
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    return n


def test_noexec_toggle_flips_shell_contract() -> None:
    """The /noexec flag changes whether citizens see the shell tool
    contract. The exact wording doesn't matter; the presence of
    the [[bash: marker name does — that's what citizens key off."""
    n = _bare_nation()

    # Default (exec on) → contract present.
    prompt_on = n._compose_system(n.agents[0]) or ""
    assert "[[bash:" in prompt_on, "exec on should expose the marker"

    # /noexec → contract gone.
    n._exec_disabled = True  # type: ignore[attr-defined]
    prompt_off = n._compose_system(n.agents[0]) or ""
    assert "[[bash:" not in prompt_off, "exec off should hide the marker"
