"""anthill citizen — lifecycle management for the nation's agents.

The verbs here are deliberately neutral. "retire" not "kill", "audit"
not "purge". Citizens that are retired stay in the nation; they just
don't get assigned new work. The user can always change their mind
with `unretire`.
"""

from __future__ import annotations

import datetime

import click
from rich.console import Console
from rich.table import Table

from anthill.config import AnthillConfig
from anthill.core.history import load_history
from anthill.core.lifecycle import (
    RetirementCriteria,
    audit_nation,
    retire_stale,
    snapshot_nation,
)
from anthill.core.persistence import load_nation, nation_dir, save_nation
from anthill.core.reproduction import (
    ReproductionCriteria,
    ancestors_of,
    auto_reproduce,
    descendants_of,
    rank_citizens,
    reproduce,
)


@click.group()
def quarantine() -> None:
    """v0.5 — list / release / set policy for the immune system."""


@quarantine.command("list")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def quarantine_list(nation_name: str) -> None:
    """Show all currently quarantined citizens."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    rows = [a for a in nation.agents if a.is_quarantined]
    if not rows:
        console.print("[dim]No citizens currently quarantined.[/dim]")
        return
    table = Table(title=f"Quarantined citizens — {nation_name}")
    table.add_column("ID", style="cyan")
    table.add_column("Model", style="magenta")
    table.add_column("Since", style="dim")
    table.add_column("Reason", style="yellow")
    for a in rows:
        when = datetime.datetime.fromtimestamp(
            a.quarantined_at or 0
        ).strftime("%m-%d %H:%M") if a.quarantined_at else "—"
        table.add_row(a.id, a.model, when, a.quarantine_reason or "—")
    console.print(table)


@quarantine.command("set")
@click.argument("agent_id")
@click.option("--reason", default="manual", help="Why you're quarantining.")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def quarantine_set(agent_id: str, reason: str, nation_name: str) -> None:
    """Manually quarantine a citizen."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    a = nation.quarantine(agent_id, reason=reason)
    if a is None:
        console.print(
            f"[red]Could not quarantine '{agent_id}' "
            f"(not found or already quarantined).[/red]"
        )
        return
    save_nation(nation, config.home)
    console.print(
        f"[yellow]Quarantined[/yellow] {a.id}  "
        f"[dim]({a.model}, reason: {a.quarantine_reason})[/dim]"
    )


@quarantine.command("release")
@click.argument("agent_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def quarantine_release(agent_id: str, nation_name: str) -> None:
    """Manually release a quarantined citizen."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    a = nation.unquarantine(agent_id)
    if a is None:
        console.print(
            f"[red]Could not release '{agent_id}' "
            f"(not found or not quarantined).[/red]"
        )
        return
    save_nation(nation, config.home)
    console.print(f"[green]Released[/green] {a.id} from quarantine.")


@quarantine.command("policy")
@click.option(
    "--auto",
    type=click.Choice(["on", "off"]),
    required=True,
    help="Turn the auto-quarantine pipeline on or off.",
)
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def quarantine_policy(auto: str, nation_name: str) -> None:
    """Enable / disable the immune system auto-quarantine."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    nation.immune_enabled = auto == "on"
    save_nation(nation, config.home)
    state = "[green]on[/green]" if nation.immune_enabled else "[dim]off[/dim]"
    console.print(f"Auto-quarantine: {state}")


# Attach as a sub-group of citizen.


console = Console()


@click.group()
def citizen() -> None:
    """Roster, lifecycle, and retirement of the nation's citizens."""


@citizen.command("list")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option("--all", "show_all", is_flag=True, help="Include retired citizens too.")
def citizen_list(nation_name: str, show_all: bool) -> None:
    """Show the nation's roster with activity + pheromone signals."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    history = load_history(nation_dir(config.home, nation_name))
    snaps = snapshot_nation(nation, history)
    if not show_all:
        snaps = [s for s in snaps if not s.is_retired]

    if not snaps:
        suffix = "" if show_all else " (use --all to show retired)"
        console.print(f"[dim]No active citizens{suffix}.[/dim]")
        return

    table = Table(title=f"Citizens — {nation_name}")
    table.add_column("ID", style="cyan")
    table.add_column("Model", style="magenta")
    table.add_column("Status")
    table.add_column("Age", justify="right")
    table.add_column("Last active", style="dim", justify="right")
    table.add_column("Max trail", justify="right", style="green")
    table.add_column("Trails", justify="right")

    color = {
        "active": "green",
        "quiet": "yellow",
        "untested": "dim",
        "retired": "red",
    }
    for s in snaps:
        label = s.status_label()
        idle = (
            f"{s.idle_days:.0f}d ago" if s.idle_days is not None else "never"
        )
        table.add_row(
            s.agent_id,
            s.model,
            f"[{color[label]}]{label}[/{color[label]}]",
            f"{s.age_days:.0f}d",
            idle,
            f"{s.max_strength:.2f}",
            str(s.task_attempts),
        )
    console.print(table)


@citizen.command("retire")
@click.argument("agent_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def citizen_retire(agent_id: str, nation_name: str) -> None:
    """Soft-delete a citizen so the router stops assigning to it."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    agent = nation.retire(agent_id)
    if agent is None:
        console.print(
            f"[red]Could not retire '{agent_id}'.[/red] "
            f"[dim](not found, or already retired)[/dim]"
        )
        return
    save_nation(nation, config.home)
    console.print(
        f"[yellow]Retired[/yellow] {agent.id}  [dim]({agent.model})[/dim]"
    )
    console.print(f"  Restore with [cyan]anthill citizen unretire {agent.id}[/cyan].")


@citizen.command("unretire")
@click.argument("agent_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def citizen_unretire(agent_id: str, nation_name: str) -> None:
    """Restore a retired citizen to active duty."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    agent = nation.unretire(agent_id)
    if agent is None:
        console.print(
            f"[red]Could not unretire '{agent_id}'.[/red] "
            f"[dim](not found, or not currently retired)[/dim]"
        )
        return
    save_nation(nation, config.home)
    console.print(f"[green]Restored[/green] {agent.id} to active duty.")


@citizen.command("audit")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option(
    "--idle-days",
    type=float,
    default=None,
    help="Minimum idle days to count as stale (default 30).",
)
@click.option(
    "--max-strength",
    type=float,
    default=None,
    help="Max pheromone strength to count as a dead trail (default 0.05).",
)
@click.option(
    "--min-age-days",
    type=float,
    default=None,
    help="Bootstrap protection — citizens younger than this are never stale (default 7).",
)
def citizen_audit(
    nation_name: str,
    idle_days: float | None,
    max_strength: float | None,
    min_age_days: float | None,
) -> None:
    """Show which citizens would be retired if you ran `retire-stale`.

    Read-only — nothing changes on disk. Tighten the thresholds via
    flags to see different cohorts; the default criteria are
    deliberately conservative.
    """
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    crit = RetirementCriteria()
    if idle_days is not None:
        crit.min_idle_days = idle_days
    if max_strength is not None:
        crit.max_dead_strength = max_strength
    if min_age_days is not None:
        crit.min_age_days = min_age_days

    history = load_history(nation_dir(config.home, nation_name))
    report = audit_nation(nation, history, crit)

    console.print(
        f"[bold]Audit[/bold] — {nation_name}  "
        f"[dim](active {report.active_count} · retired {report.retired_count})[/dim]"
    )
    console.print(
        f"[dim]criteria: idle ≥ {crit.min_idle_days:.0f}d · "
        f"max trail ≤ {crit.max_dead_strength:.2f} · "
        f"min age {crit.min_age_days:.0f}d[/dim]"
    )
    console.print()

    if not report.stale:
        console.print("[green]No stale citizens by these criteria.[/green]")
        return

    table = Table(title="Would retire")
    table.add_column("ID", style="cyan")
    table.add_column("Model", style="magenta")
    table.add_column("Age", justify="right")
    table.add_column("Idle", justify="right")
    table.add_column("Max trail", justify="right")
    for s in report.stale:
        idle = f"{s.idle_days:.0f}d" if s.idle_days is not None else "never"
        table.add_row(
            s.agent_id,
            s.model,
            f"{s.age_days:.0f}d",
            idle,
            f"{s.max_strength:.2f}",
        )
    console.print(table)
    console.print()
    console.print(
        "[dim]Apply with [cyan]anthill citizen retire-stale[/cyan] "
        "(use the same threshold flags).[/dim]"
    )


@citizen.command("retire-stale")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option("--idle-days", type=float, default=None, help="Override default 30d.")
@click.option("--max-strength", type=float, default=None, help="Override default 0.05.")
@click.option("--min-age-days", type=float, default=None, help="Override default 7d.")
@click.option(
    "--yes", is_flag=True, help="Skip the confirmation prompt.",
)
def citizen_retire_stale(
    nation_name: str,
    idle_days: float | None,
    max_strength: float | None,
    min_age_days: float | None,
    yes: bool,
) -> None:
    """Run the audit and retire every citizen it flags."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    crit = RetirementCriteria()
    if idle_days is not None:
        crit.min_idle_days = idle_days
    if max_strength is not None:
        crit.max_dead_strength = max_strength
    if min_age_days is not None:
        crit.min_age_days = min_age_days

    history = load_history(nation_dir(config.home, nation_name))
    report = audit_nation(nation, history, crit)
    if not report.stale:
        console.print("[green]Nothing to retire.[/green]")
        return

    console.print(f"[yellow]Would retire {len(report.stale)} citizen(s):[/yellow]")
    for s in report.stale:
        console.print(f"  - {s.agent_id} ({s.model})")
    if not yes:
        if not click.confirm("Proceed?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    retired = retire_stale(nation, history, crit)
    save_nation(nation, config.home)
    console.print(f"[yellow]Retired {len(retired)} citizen(s).[/yellow]")
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    console.print(f"[dim]({when})[/dim]")


@citizen.command("rank")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option(
    "--min-fitness", type=float, default=None,
    help="Override the qualifying fitness threshold (default 0.5).",
)
def citizen_rank(nation_name: str, min_fitness: float | None) -> None:
    """Rank citizens by fitness — who would qualify to reproduce."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    crit = ReproductionCriteria()
    if min_fitness is not None:
        crit.min_fitness = min_fitness

    scores = rank_citizens(nation, crit)
    if not scores:
        console.print("[dim]No citizens to rank.[/dim]")
        return

    table = Table(title=f"Citizen fitness — {nation_name}")
    table.add_column("ID", style="cyan")
    table.add_column("Model", style="magenta")
    table.add_column("Fitness", style="green", justify="right")
    table.add_column("Trails", justify="right")
    table.add_column("Qualifies")
    table.add_column("Notes", style="dim")
    for s in scores:
        ok = "[green]yes[/green]" if s.qualifies else "[dim]no[/dim]"
        table.add_row(
            s.agent_id,
            s.model,
            f"{s.score:.2f}",
            str(s.task_type_count),
            ok,
            s.reason(),
        )
    console.print(table)
    console.print(
        f"[dim]threshold: fitness ≥ {crit.min_fitness:.2f}, "
        f"task_types ≥ {crit.min_task_types}[/dim]"
    )


@citizen.command("reproduce")
@click.argument("agent_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def citizen_reproduce(agent_id: str, nation_name: str) -> None:
    """Spawn a mutated child of a specific citizen."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    parent = nation.find_agent(agent_id)
    if parent is None:
        console.print(f"[red]No citizen matching '{agent_id}'.[/red]")
        return
    if parent.is_retired:
        console.print(
            f"[yellow]Refusing to reproduce a retired citizen.[/yellow] "
            f"[dim](unretire {parent.id} first if you really want this.)[/dim]"
        )
        return

    lineage = reproduce(nation, parent)
    save_nation(nation, config.home)
    console.print(
        f"[green]Born[/green] {lineage.child.id}  "
        f"[dim](generation {lineage.child.generation}, "
        f"mutation: {lineage.mutation.name})[/dim]"
    )
    for note in lineage.notes:
        console.print(f"  • {note}")
    console.print(f"[dim]parent: {parent.id}[/dim]")


@citizen.command("reproduce-fit")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option(
    "--min-fitness", type=float, default=None, help="Override default 0.5."
)
@click.option(
    "--max-births", type=int, default=None,
    help="Cap the total number of children spawned in this pass.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def citizen_reproduce_fit(
    nation_name: str,
    min_fitness: float | None,
    max_births: int | None,
    yes: bool,
) -> None:
    """Reproduce every qualifying citizen in one pass."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    crit = ReproductionCriteria()
    if min_fitness is not None:
        crit.min_fitness = min_fitness

    ranked = rank_citizens(nation, crit)
    qualifiers = [s for s in ranked if s.qualifies]
    if max_births is not None:
        qualifiers = qualifiers[:max_births]
    if not qualifiers:
        console.print("[yellow]No citizens qualify for reproduction.[/yellow]")
        return

    console.print(f"[bold]Would reproduce {len(qualifiers)} citizen(s):[/bold]")
    for s in qualifiers:
        console.print(f"  - {s.agent_id} ({s.model})  fitness {s.score:.2f}")
    if not yes:
        if not click.confirm("Proceed?"):
            console.print("[dim]Cancelled.[/dim]")
            return

    lineages = auto_reproduce(nation, criteria=crit, max_births=max_births)
    save_nation(nation, config.home)
    console.print(f"[green]Spawned {len(lineages)} child(ren).[/green]")
    for lin in lineages:
        console.print(
            f"  • {lin.child.id}  [dim](parent {lin.parent.id}, "
            f"mutation {lin.mutation.name})[/dim]"
        )


@citizen.command("family")
@click.argument("agent_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def citizen_family(agent_id: str, nation_name: str) -> None:
    """Show the ancestry and descendants of one citizen."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    target = nation.find_agent(agent_id)
    if target is None:
        console.print(f"[red]No citizen matching '{agent_id}'.[/red]")
        return

    ancestors = ancestors_of(nation, target.id)
    descendants = descendants_of(nation, target.id)

    console.print(
        f"[bold]{target.id}[/bold] [dim]({target.model}, "
        f"generation {target.generation})[/dim]"
    )
    console.print()

    if ancestors:
        console.print("[bold]Ancestors[/bold] (oldest first)")
        for a in reversed(ancestors):
            retired = " [red](retired)[/red]" if a.is_retired else ""
            console.print(
                f"  ↑ {a.id} [dim]({a.model}, gen {a.generation})[/dim]{retired}"
            )
    else:
        console.print("[dim]No recorded ancestors — likely a founder.[/dim]")

    console.print()
    if descendants:
        console.print(f"[bold]Descendants[/bold] ({len(descendants)})")
        for d in descendants:
            retired = " [red](retired)[/red]" if d.is_retired else ""
            console.print(
                f"  ↓ {d.id} [dim]({d.model}, gen {d.generation})[/dim]{retired}"
            )
    else:
        console.print("[dim]No descendants yet.[/dim]")


citizen.add_command(quarantine)
