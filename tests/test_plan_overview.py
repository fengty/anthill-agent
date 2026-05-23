"""0.2.13 — multi-model routing preview before execution.

The view we want to protect:
  📋 Scout 拆成 3 步: research → analyze → summarize
     预计路由: research → deepseek (0.85), analyze → minimax (0.78),
              summarize → 探索 (cold)

Tests focus on contracts that would actually break UX:
  - Empty plan stays silent (no garbage on screen)
  - Routing line surfaces MODEL names + cold-start label
  - No-agents nation doesn't crash
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.scout import Plan, Subtask


def _render(plan, nation) -> str:
    import anthill.cli.repl as repl_mod

    buf = StringIO()
    fake = Console(file=buf, force_terminal=False, width=120)
    original = repl_mod.console
    repl_mod.console = fake
    try:
        repl_mod._print_plan_overview(plan, nation)
    finally:
        repl_mod.console = original
    return buf.getvalue()


def test_empty_plan_silent() -> None:
    """Don't pollute the screen when there's no plan."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="deepseek")]
    assert _render(Plan(subtasks=[]), n) == ""


def test_routing_preview_shows_models_and_cold_label() -> None:
    """The single behavior that makes the preview useful:
      - subtasks with pheromone trails show the MODEL name
      - subtasks without a trail show 探索 (cold), not '0.00'
    Together these tell the user 'this part has confidence, that
    part is exploration.'"""
    n = Nation(name="t")
    n.agents = [
        Agent(id="ant-1", model="deepseek"),
        Agent(id="ant-2", model="minimax"),
    ]
    for _ in range(5):
        n.pheromones.deposit("ant-1", "research", success_score=1.0)
    plan = Plan(subtasks=[
        Subtask("research", "x", []),
        Subtask("brand_new", "y", ["research"]),
    ])
    out = _render(plan, n)
    # Has-trail subtask: model name appears.
    assert "deepseek" in out
    # No-trail subtask: cold label, not bogus 0.00.
    assert "探索" in out
    assert "0.00" not in out


def test_no_agents_doesnt_crash() -> None:
    """Defensive: empty nation should still render the headline,
    not blow up on routing lookup."""
    n = Nation(name="t")
    n.agents = []
    plan = Plan(subtasks=[Subtask("research", "x", [])])
    out = _render(plan, n)
    assert "Scout" in out
