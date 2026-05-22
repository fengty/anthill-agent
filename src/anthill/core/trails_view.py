"""0.2.3 — make pheromone learning visible.

anthill's central differentiator is pheromone-routed multi-model
collaboration: the nation actually LEARNS which model is good at
what. Pre-0.2.3 the `/trails` REPL command showed a flat sorted
table — accurate but doesn't tell the story. This module renders
the same data as the heat map the pheromone metaphor invites:

  task_type \\ citizen       ant-1(deepseek)  ant-2(minimax)  ant-3(claude)
  research                          0.86           0.42            0.71
  analyze                           0.55           0.78            0.62
  summarize                         0.31           0.62            0.55

Plus utilities for the per-ask decision explanation:

  "Scout picked ant-2/minimax for analyze (trail 0.78, was 0.62
   last week, +N samples)."

Hermes is single-agent and has no equivalent surface — this is the
"anthill is visibly different" play.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from anthill.core.agent import Agent
from anthill.core.pheromone import PheromoneTrail


@dataclass(frozen=True)
class HeatmapCell:
    """One (citizen, task_type) cell in the heatmap."""

    agent_id: str
    agent_model: str
    task_type: str
    strength: float
    alarm: float
    sample_count: int  # how many attempts have updated this trail


@dataclass(frozen=True)
class TaskTypeRanking:
    """For one task_type: every citizen ranked by net trail."""

    task_type: str
    cells: list[HeatmapCell]  # sorted desc by net


def build_heatmap(
    pheromones: PheromoneTrail,
    agents: list[Agent],
) -> tuple[list[str], list[Agent], dict[tuple[str, str], HeatmapCell]]:
    """Return (task_types, agents, cell_lookup) for rendering.

    `cell_lookup` is keyed by (agent_id, task_type). When a cell is
    missing (no trail), the caller draws an empty/blank cell — we
    don't synthesize fake zeros (that would imply we tested and
    failed, vs. just never tried).

    Task types and agents are returned in a stable display order:
    task types sorted by total net strength (most-trodden first),
    agents sorted by overall net (winners on top). That way the
    most interesting cell is in the top-left.
    """
    cells: dict[tuple[str, str], HeatmapCell] = {}
    by_model = {a.id: a.model for a in agents}

    # Build cell lookup + sample counts. PheromoneTrail.trails()
    # yields one Trail per (agent_id, task_type) that's been touched.
    for trail in pheromones.trails():
        model = by_model.get(trail.agent_id, "?")
        # Sample count isn't first-class on Trail; approximate from
        # strength as integer "this many successful reinforcements"
        # — useful enough for the UI.
        sample_count = max(
            1,
            int(round(trail.strength + trail.alarm)),
        )
        cells[(trail.agent_id, trail.task_type)] = HeatmapCell(
            agent_id=trail.agent_id,
            agent_model=model,
            task_type=trail.task_type,
            strength=trail.strength,
            alarm=trail.alarm,
            sample_count=sample_count,
        )

    # Stable orderings.
    task_totals: dict[str, float] = {}
    agent_totals: dict[str, float] = {}
    for c in cells.values():
        task_totals[c.task_type] = task_totals.get(c.task_type, 0.0) + max(
            0.0, c.strength - c.alarm
        )
        agent_totals[c.agent_id] = agent_totals.get(c.agent_id, 0.0) + max(
            0.0, c.strength - c.alarm
        )

    task_types = sorted(
        task_totals.keys(),
        key=lambda t: task_totals[t],
        reverse=True,
    )
    # Agents in display order: those with any trail first (sorted by
    # total), then untouched ones at the end (so empty rows don't
    # bury the action).
    touched = sorted(
        (a for a in agents if a.id in agent_totals),
        key=lambda a: agent_totals[a.id],
        reverse=True,
    )
    untouched = [a for a in agents if a.id not in agent_totals]
    ordered_agents = touched + untouched

    return task_types, ordered_agents, cells


def rank_for_task(
    pheromones: PheromoneTrail,
    agents: list[Agent],
    task_type: str,
) -> TaskTypeRanking:
    """Per-task drill-in: who's #1, #2, #3 at this task_type."""
    by_model = {a.id: a.model for a in agents}
    cells_for_task: list[HeatmapCell] = []
    for trail in pheromones.trails():
        if trail.task_type != task_type:
            continue
        cells_for_task.append(
            HeatmapCell(
                agent_id=trail.agent_id,
                agent_model=by_model.get(trail.agent_id, "?"),
                task_type=task_type,
                strength=trail.strength,
                alarm=trail.alarm,
                sample_count=max(
                    1, int(round(trail.strength + trail.alarm))
                ),
            )
        )
    cells_for_task.sort(
        key=lambda c: max(0.0, c.strength - c.alarm),
        reverse=True,
    )
    return TaskTypeRanking(task_type=task_type, cells=cells_for_task)


def explain_routing_decision(
    pheromones: PheromoneTrail,
    agents: list[Agent],
    chosen_agent_id: str,
    task_type: str,
) -> str:
    """One-line "why this citizen?" for the post-ask trace.

    Shapes:
      "trail 0.85 (top of N)"            — winner pick
      "trail 0.42 (#3 of N)"              — non-top but ranked
      "exploration pick (no prior data)"  — cold start
      "exploration pick (off the top)"    — 10% noise picked a non-#1
    """
    ranking = rank_for_task(pheromones, agents, task_type)
    if not ranking.cells:
        return "exploration pick (no prior data)"

    # Find chosen cell.
    chosen_cell = None
    chosen_rank = None
    for i, cell in enumerate(ranking.cells, start=1):
        if cell.agent_id == chosen_agent_id:
            chosen_cell = cell
            chosen_rank = i
            break
    if chosen_cell is None:
        # The chosen agent has no trail in this task_type → cold start
        return "exploration pick (no prior data)"
    if chosen_rank == 1:
        return f"trail {chosen_cell.strength:.2f} (top of {len(ranking.cells)})"
    return (
        f"trail {chosen_cell.strength:.2f} "
        f"(#{chosen_rank} of {len(ranking.cells)}, exploration pick)"
    )


def cell_intensity_label(strength: float) -> tuple[str, str]:
    """For terminal rendering: (color_name, label_char) per strength bucket.

    Five buckets so the heat map reads at a glance:
      0.00–0.20  cold (gray dot)
      0.21–0.40  cool (cyan)
      0.41–0.60  mid  (yellow)
      0.61–0.80  warm (orange — rich uses 'dark_orange')
      0.81+      hot  (red — highest learned strength)
    """
    if strength < 0.21:
        return ("dim", "·")
    if strength < 0.41:
        return ("cyan", "▂")
    if strength < 0.61:
        return ("yellow", "▄")
    if strength < 0.81:
        return ("dark_orange", "▆")
    return ("red", "█")


def format_cell(cell: HeatmapCell | None, *, max_chars: int = 7) -> str:
    """Render one heatmap cell as 'X.XX' or '·' for missing."""
    if cell is None:
        return "·".rjust(max_chars)
    return f"{cell.strength:.2f}".rjust(max_chars)


def total_samples_across_trails(
    pheromones: PheromoneTrail,
) -> int:
    """Used by the header line: 'N samples observed across M trails'."""
    total = 0
    count = 0
    for trail in pheromones.trails():
        count += 1
        total += max(1, int(round(trail.strength + trail.alarm)))
    return total


def trails_summary_line(
    pheromones: PheromoneTrail, agents: Iterable[Agent]
) -> str:
    """One-line summary printed under the heatmap title."""
    task_types, _, cells = build_heatmap(pheromones, list(agents))
    n_trails = len(cells)
    n_samples = total_samples_across_trails(pheromones)
    n_tasks = len(task_types)
    return (
        f"{n_trails} trail(s) across {n_tasks} task_type(s), "
        f"~{n_samples} observations recorded."
    )
