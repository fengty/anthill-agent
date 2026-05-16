"""anthill values — inspect & nudge the open-vocabulary quality dimensions.

There is no fixed dimension list to "configure." The dimensions appear
on their own — the LLM judge invents them and the user reinforces
them by calling `anthill rate --dim ...` (lands in v0.4.x).

The commands here are observational and weight-tuning only. They
should never *create* a dimension out of nothing — the catalog has
to have seen a dimension in actual scoring before the user can
re-weight it. That keeps the mechanism honest: dimensions exist
because the work showed they mattered, not because the user typed
a wish into a config file.
"""

from __future__ import annotations

import datetime

import click
from rich.console import Console
from rich.table import Table

from anthill.config import AnthillConfig
from anthill.core.persistence import load_nation, save_nation
from anthill.core.values import normalize_dim


console = Console()


@click.group()
def values() -> None:
    """Inspect & re-weight the nation's quality dimensions."""


@values.command("show")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def values_show(nation_name: str) -> None:
    """Print every dimension the nation has ever observed."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    cat = nation.dimension_catalog
    if not cat.dimensions:
        console.print(
            "[dim]No dimensions observed yet. They appear as the judge or "
            "`anthill rate --dim` reports scores along them.[/dim]"
        )
        return

    table = Table(title=f"Quality dimensions — {nation_name}")
    table.add_column("Dimension", style="cyan")
    table.add_column("Avg", justify="right", style="green")
    table.add_column("Weight", justify="right")
    table.add_column("Obs", justify="right")
    table.add_column("Last seen", style="dim")
    table.add_column("Description", style="dim")

    for key in cat.known():
        d = cat.dimensions[key]
        when = datetime.datetime.fromtimestamp(d.last_seen).strftime("%m-%d %H:%M")
        w = cat.weight(key)
        weight_str = f"{w:.2f}" if w != 1.0 else "—"
        table.add_row(
            d.name,
            f"{d.avg_score:.2f}",
            weight_str,
            str(d.observations),
            when,
            d.description[:50] if d.description else "—",
        )
    console.print(table)
    if cat.weights:
        console.print(
            "[dim]Non-default weights are applied when the router/judge "
            "combine dimension scores. Reset with [cyan]anthill values "
            "reset-weights[/cyan].[/dim]"
        )


@values.command("weight")
@click.argument("dimension")
@click.argument("value", type=float)
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def values_weight(dimension: str, value: float, nation_name: str) -> None:
    """Set the relative weight of one dimension.

    Refuses to weight a dimension the nation hasn't observed — the
    mechanism only knows dimensions that actually showed up in work.
    """
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    key = normalize_dim(dimension)
    if key not in nation.dimension_catalog.dimensions:
        console.print(
            f"[red]Dimension '{dimension}' has not been observed in this "
            f"nation yet.[/red]"
        )
        console.print(
            "[dim]Dimensions appear after scoring. Run some asks with "
            "`use_judge=True` or use `anthill rate --dim ...` first.[/dim]"
        )
        return

    nation.dimension_catalog.set_weight(key, value)
    save_nation(nation, config.home)
    console.print(
        f"[green]Set weight[/green] {key} = {value:.2f}  "
        f"[dim](default 1.0)[/dim]"
    )


@values.command("reset-weights")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def values_reset_weights(nation_name: str) -> None:
    """Drop all custom dimension weights (back to flat 1.0 each)."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    n_dropped = len(nation.dimension_catalog.weights)
    nation.dimension_catalog.reset_weights()
    save_nation(nation, config.home)
    console.print(f"[green]Cleared[/green] {n_dropped} custom weight(s).")


@values.command("show-citizen")
@click.argument("agent_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def values_show_citizen(agent_id: str, nation_name: str) -> None:
    """Show one citizen's per-(task_type, dimension) trail scores.

    Useful for asking "who's actually good at conciseness on summarize
    tasks?" without having to grep pheromones.json by hand.
    """
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    agent = nation.find_agent(agent_id)
    if agent is None:
        console.print(f"[red]No citizen matching '{agent_id}'.[/red]")
        return

    rows = []
    for trail in nation.pheromones.trails():
        if trail.agent_id != agent.id:
            continue
        if not trail.dim_scores:
            continue
        for dim, score in sorted(trail.dim_scores.items()):
            rows.append((trail.task_type, dim, score))
    if not rows:
        console.print(
            f"[dim]{agent.id} has no per-dimension scores yet.[/dim]"
        )
        return

    table = Table(title=f"{agent.id} — dimension scores by task type")
    table.add_column("Task type", style="magenta")
    table.add_column("Dimension", style="cyan")
    table.add_column("Score", justify="right", style="green")
    for tt, dim, score in rows:
        table.add_row(tt, dim, f"{score:.2f}")
    console.print(table)
