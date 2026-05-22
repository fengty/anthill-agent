"""0.2.3 — pheromone visualization tests.

The view layer is pure: takes a PheromoneTrail + agent list, returns
sorted display data. No console / no rich. These tests cover:
  - heatmap ordering (most-trodden task first, strongest citizen first)
  - per-task drill-in ranking
  - routing decision explanation
  - cell intensity buckets
  - empty / cold-start cases
"""

from __future__ import annotations

from anthill.core.agent import Agent
from anthill.core.pheromone import PheromoneTrail
from anthill.core.trails_view import (
    HeatmapCell,
    build_heatmap,
    cell_intensity_label,
    explain_routing_decision,
    format_cell,
    rank_for_task,
    total_samples_across_trails,
    trails_summary_line,
)


def _setup(strengths: dict[tuple[str, str], float]) -> tuple[PheromoneTrail, list[Agent]]:
    """Build a PheromoneTrail + agents from a (agent_id, task_type)→strength dict.

    The test fixture: 3 agents on different models; trails are seeded
    manually via the public reinforce path.
    """
    agents = [
        Agent(id="ant-1", model="deepseek"),
        Agent(id="ant-2", model="minimax"),
        Agent(id="ant-3", model="claude"),
    ]
    trail = PheromoneTrail()
    for (aid, tt), s in strengths.items():
        # Deposit N times to approximate the strength.
        for _ in range(max(1, int(round(s * 5)))):
            trail.deposit(aid, tt, success_score=1.0)
    return trail, agents


# --- build_heatmap -------------------------------------------------------


def test_build_heatmap_empty_pheromones() -> None:
    """No trails recorded yet → empty cells dict, but agent list intact."""
    trail = PheromoneTrail()
    agents = [Agent(id="ant-1", model="deepseek")]
    task_types, ordered, cells = build_heatmap(trail, agents)
    assert task_types == []
    assert ordered == agents  # untouched agents come last == only agents
    assert cells == {}


def test_build_heatmap_orders_tasks_by_total_strength() -> None:
    """Most-trodden task type appears first (top row)."""
    trail, agents = _setup(
        {
            ("ant-1", "low_traffic"): 0.2,
            ("ant-1", "high_traffic"): 0.9,
            ("ant-2", "high_traffic"): 0.8,
            ("ant-2", "mid_traffic"): 0.5,
        }
    )
    task_types, _, _ = build_heatmap(trail, agents)
    # high_traffic has the most weight, then mid, then low.
    assert task_types[0] == "high_traffic"
    assert task_types[-1] == "low_traffic"


def test_build_heatmap_orders_agents_by_total_then_untouched_last() -> None:
    """Strong agents at the left; agents with no trails at the end."""
    trail, agents = _setup(
        {
            ("ant-2", "research"): 0.95,
            ("ant-1", "research"): 0.30,
            # ant-3 has no trails.
        }
    )
    _, ordered, _ = build_heatmap(trail, agents)
    assert ordered[0].id == "ant-2"  # strongest first
    assert ordered[1].id == "ant-1"
    assert ordered[-1].id == "ant-3"  # untouched last


def test_build_heatmap_cell_has_model_name() -> None:
    """The cell remembers which model the agent runs on — used for
    the heatmap header line."""
    trail, agents = _setup({("ant-2", "research"): 0.5})
    _, _, cells = build_heatmap(trail, agents)
    cell = cells[("ant-2", "research")]
    assert cell.agent_model == "minimax"


# --- rank_for_task -------------------------------------------------------


def test_rank_for_task_orders_by_net() -> None:
    trail, agents = _setup(
        {
            ("ant-1", "research"): 0.8,
            ("ant-2", "research"): 0.5,
            ("ant-3", "research"): 0.9,
        }
    )
    ranking = rank_for_task(trail, agents, "research")
    assert [c.agent_id for c in ranking.cells] == ["ant-3", "ant-1", "ant-2"]


def test_rank_for_task_unknown_task_returns_empty() -> None:
    trail, agents = _setup({("ant-1", "research"): 0.5})
    ranking = rank_for_task(trail, agents, "never_seen")
    assert ranking.cells == []


def test_rank_for_task_reports_correct_task_type() -> None:
    trail, agents = _setup({("ant-1", "research"): 0.5})
    ranking = rank_for_task(trail, agents, "research")
    assert ranking.task_type == "research"


# --- explain_routing_decision -------------------------------------------


def test_explain_top_pick() -> None:
    """Winner of the trail → 'trail X.XX (top of N)'."""
    trail, agents = _setup(
        {
            ("ant-1", "research"): 0.9,
            ("ant-2", "research"): 0.4,
        }
    )
    msg = explain_routing_decision(trail, agents, "ant-1", "research")
    assert "trail" in msg
    assert "top" in msg
    assert "of 2" in msg


def test_explain_non_top_pick() -> None:
    """Non-#1 winner → '#N of M, exploration pick'."""
    trail, agents = _setup(
        {
            ("ant-1", "research"): 0.9,
            ("ant-2", "research"): 0.4,
            ("ant-3", "research"): 0.7,
        }
    )
    msg = explain_routing_decision(trail, agents, "ant-2", "research")
    # ant-2 has the weakest trail → #3
    assert "#3" in msg
    assert "exploration" in msg


def test_explain_cold_start() -> None:
    """No trail data for this task_type → 'no prior data'."""
    trail, agents = _setup({})
    msg = explain_routing_decision(trail, agents, "ant-1", "research")
    assert "no prior data" in msg


def test_explain_chosen_agent_unknown_trail() -> None:
    """Chosen agent has no trail in this task_type (but others do) →
    cold start path. This case happens after `forbid` shifts routing
    onto an agent that's never tried this work."""
    trail, agents = _setup(
        {("ant-1", "research"): 0.9}  # only ant-1 has a trail
    )
    msg = explain_routing_decision(trail, agents, "ant-2", "research")
    assert "no prior data" in msg


# --- cell_intensity_label -----------------------------------------------


def test_cell_intensity_buckets() -> None:
    """Five color buckets cover the [0,1] strength range."""
    assert cell_intensity_label(0.05)[0] == "dim"
    assert cell_intensity_label(0.30)[0] == "cyan"
    assert cell_intensity_label(0.50)[0] == "yellow"
    assert cell_intensity_label(0.70)[0] == "dark_orange"
    assert cell_intensity_label(0.95)[0] == "red"


def test_cell_intensity_bucket_boundaries() -> None:
    """Boundary values land in the lower bucket consistently."""
    assert cell_intensity_label(0.21)[0] == "cyan"
    assert cell_intensity_label(0.41)[0] == "yellow"
    assert cell_intensity_label(0.61)[0] == "dark_orange"
    assert cell_intensity_label(0.81)[0] == "red"


# --- format_cell --------------------------------------------------------


def test_format_cell_with_data() -> None:
    cell = HeatmapCell(
        agent_id="ant-1",
        agent_model="x",
        task_type="t",
        strength=0.85,
        alarm=0.0,
        sample_count=3,
    )
    assert "0.85" in format_cell(cell)


def test_format_cell_none() -> None:
    """Missing cell rendered as a dot — we DON'T fake a 0.0."""
    assert "·" in format_cell(None)


# --- summary helpers ----------------------------------------------------


def test_trails_summary_line_zero() -> None:
    trail = PheromoneTrail()
    agents = [Agent(id="ant-1", model="deepseek")]
    line = trails_summary_line(trail, agents)
    assert "0 trail" in line


def test_trails_summary_line_populated() -> None:
    trail, agents = _setup(
        {
            ("ant-1", "research"): 0.6,
            ("ant-1", "analyze"): 0.4,
            ("ant-2", "research"): 0.3,
        }
    )
    line = trails_summary_line(trail, agents)
    assert "3 trail" in line
    assert "2 task_type" in line


def test_total_samples_across_trails_counts_observations() -> None:
    trail, _ = _setup({("ant-1", "research"): 1.0})
    # strength≈1.0 reinforced 5 times → ~5 samples.
    n = total_samples_across_trails(trail)
    assert n >= 4
