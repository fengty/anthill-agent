"""0.2.13 — multi-model routing preview before execution.

Anthill's core differentiator is "multiple models collaborate on
one ask." Before this version, that story only appeared in the
post-execution trace (after 30s of streaming). Now it appears
BEFORE subtasks start:

  📋 Scout 拆成 3 步: research → analyze → summarize
     预计路由: research → deepseek (0.85), analyze → minimax (0.78),
              summarize → 探索 (cold)

Tests cover:
  - Empty plan → no print (don't pollute the screen)
  - Single-subtask plan → still shows the chain (1 task)
  - Multi-subtask plan → chain + per-task routing line
  - Cold-start citizen → '探索' label (not '0.00')
  - Routing line picks up agent.model, not just id
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.scout import Plan, Subtask


def _render_overview(plan, nation) -> str:
    """Run _print_plan_overview against a captured console and
    return the rendered string. Patches the module-level console."""
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


def _nation_with_pheromone(seeds: dict[tuple[str, str], float]) -> Nation:
    """Build a nation with primed pheromone trails for test predictability."""
    n = Nation(name="t")
    # Distinct models so the routing line is visually rich.
    n.agents = [
        Agent(id="ant-1", model="deepseek"),
        Agent(id="ant-2", model="minimax"),
        Agent(id="ant-3", model="claude"),
    ]
    for (aid, tt), strength in seeds.items():
        # Multiple deposits to push the trail to roughly the strength.
        for _ in range(max(1, int(round(strength * 5)))):
            n.pheromones.deposit(aid, tt, success_score=1.0)
    return n


# --- empty / trivial -----------------------------------------------------


def test_empty_plan_prints_nothing() -> None:
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="deepseek")]
    out = _render_overview(Plan(subtasks=[]), n)
    assert out == ""


# --- chain rendering -----------------------------------------------------


def test_single_subtask_chain() -> None:
    n = _nation_with_pheromone({("ant-1", "research"): 0.8})
    plan = Plan(subtasks=[Subtask("research", "do it", [])])
    out = _render_overview(plan, n)
    # Headline.
    assert "Scout 拆成 1 步" in out
    assert "research" in out


def test_three_subtask_chain_with_arrows() -> None:
    n = _nation_with_pheromone(
        {
            ("ant-1", "research"): 0.85,
            ("ant-2", "analyze"): 0.78,
            ("ant-3", "summarize"): 0.60,
        }
    )
    plan = Plan(
        subtasks=[
            Subtask("research", "do x", []),
            Subtask("analyze", "do y", ["research"]),
            Subtask("summarize", "do z", ["analyze"]),
        ]
    )
    out = _render_overview(plan, n)
    assert "Scout 拆成 3 步" in out
    # Chain text shows all three task_types.
    for tt in ("research", "analyze", "summarize"):
        assert tt in out


# --- routing preview line ----------------------------------------------


def test_routing_line_shows_model_name() -> None:
    """The point: user sees 'deepseek' / 'minimax' / 'claude', not
    just opaque agent IDs."""
    n = _nation_with_pheromone(
        {
            ("ant-1", "research"): 0.85,
            ("ant-2", "analyze"): 0.78,
        }
    )
    plan = Plan(
        subtasks=[
            Subtask("research", "do x", []),
            Subtask("analyze", "do y", ["research"]),
        ]
    )
    out = _render_overview(plan, n)
    # Models appear in the routing preview line.
    assert "deepseek" in out
    assert "minimax" in out
    # Some strength figure appears so the user sees WHY this routing.
    # Pheromone strength accumulates with deposits — we just check
    # SOME positive numeric appears, not an exact value.
    import re as _re
    assert _re.search(r"\d+\.\d{2}", out)


def test_routing_line_shows_cold_label_when_no_trail() -> None:
    """A task_type with no trail data yet should show '探索' not '0.00'."""
    n = _nation_with_pheromone({})  # no trails seeded
    plan = Plan(subtasks=[Subtask("brand_new_task", "x", [])])
    out = _render_overview(plan, n)
    assert "探索" in out
    # And NOT '0.00' (that would imply we tried and failed).
    assert "0.00" not in out


def test_routing_line_per_task_independent() -> None:
    """Mixed: some task_types have trails, others are cold. Each
    line should reflect its own state."""
    n = _nation_with_pheromone({("ant-1", "research"): 0.9})
    plan = Plan(
        subtasks=[
            Subtask("research", "x", []),
            Subtask("brand_new", "y", ["research"]),
        ]
    )
    out = _render_overview(plan, n)
    assert "deepseek" in out  # research has a trail
    assert "探索" in out  # brand_new is cold


# --- defensiveness ------------------------------------------------------


def test_no_agents_doesnt_crash() -> None:
    """Edge case: a nation with no citizens shouldn't crash the
    overview. Routing preview just shows cold for every task."""
    n = Nation(name="t")
    n.agents = []
    plan = Plan(subtasks=[Subtask("research", "x", [])])
    out = _render_overview(plan, n)
    # Should still print the headline.
    assert "Scout 拆成 1 步" in out
