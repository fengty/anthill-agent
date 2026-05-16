"""anthill nation <subcommand> — manage your nations from the CLI.

  anthill nation              same as list
  anthill nation list         show all nations + which one is current
  anthill nation create NAME  found a fresh empty nation
  anthill nation show NAME    summary card
  anthill nation switch NAME  mark NAME as the current nation
  anthill nation rename A B   rename a nation directory + state
  anthill nation remove NAME  delete (with confirmation)
"""

from __future__ import annotations

import shutil

import click
from rich.console import Console
from rich.table import Table

from anthill.config import AnthillConfig
from anthill.core.history import load_history
from anthill.core.nation import Nation
from anthill.core.persistence import load_nation, nation_dir, save_nation
from anthill.core.power import compute_power
from anthill.core.router import RouterConfig
from anthill.core.userconfig import load_config


console = Console()


def _current_pointer_path(home):  # noqa: ANN001
    return home / "current_nation"


def _current_nation_name(home) -> str:  # noqa: ANN001
    path = _current_pointer_path(home)
    if path.exists():
        return path.read_text().strip() or "default"
    return "default"


def _set_current_nation(home, name: str) -> None:  # noqa: ANN001
    path = _current_pointer_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name)


def _list_nations(home) -> list[str]:  # noqa: ANN001
    base = home / "nations"
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


@click.group(invoke_without_command=True)
@click.pass_context
def nation(ctx: click.Context) -> None:
    """Manage your nations."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(nation_list)


@nation.command("list")
def nation_list() -> None:
    """List all nations + show which is current."""
    config = AnthillConfig.load()
    names = _list_nations(config.home)
    if not names:
        console.print(
            "[dim]No nations yet.[/dim] "
            "Run [cyan]anthill setup[/cyan] or [cyan]anthill nation create NAME[/cyan]."
        )
        return
    current = _current_nation_name(config.home)
    table = Table(title="Nations")
    table.add_column("Name", style="cyan")
    table.add_column("Citizens", justify="right")
    table.add_column("Trails", justify="right")
    table.add_column("Asks", justify="right")
    table.add_column("Current", style="green", justify="center")
    for name in names:
        loaded = load_nation(name, config.home)
        if loaded is None:
            continue
        citizens = len(loaded.agents)
        trails = sum(1 for _ in loaded.pheromones.trails())
        history = load_history(nation_dir(config.home, name))
        marker = "★" if name == current else ""
        table.add_row(name, str(citizens), str(trails), str(len(history)), marker)
    console.print(table)


@nation.command("create")
@click.argument("name")
@click.option("--citizens", default=0, help="Spawn N citizens immediately.")
def nation_create(name: str, citizens: int) -> None:
    """Found a new empty nation."""
    config = AnthillConfig.load()
    if (config.home / "nations" / name).exists():
        console.print(f"[red]Nation '{name}' already exists.[/red]")
        raise SystemExit(1)
    nation_obj = Nation(
        name=name,
        router_config=RouterConfig(exploration=config.exploration_rate),
    )
    if citizens > 0:
        user_cfg = load_config()
        model = user_cfg.default_model or config.default_model
        nation_obj.spawn(count=citizens, model=model)
    save_nation(nation_obj, config.home)
    console.print(
        f"[green]✓[/green] Founded '{name}' "
        f"({len(nation_obj.agents)} citizen{'s' if len(nation_obj.agents) != 1 else ''})."
    )


@nation.command("show")
@click.argument("name")
def nation_show(name: str) -> None:
    """Show details for one nation."""
    config = AnthillConfig.load()
    nation_obj = load_nation(name, config.home)
    if nation_obj is None:
        console.print(f"[red]No nation named '{name}'.[/red]")
        raise SystemExit(1)
    history = load_history(nation_dir(config.home, name))
    report = compute_power(nation_obj, history, [])
    console.print(f"[bold]{name}[/bold]")
    console.print(f"  citizens:     {len(nation_obj.agents)}")
    console.print(f"  trails:       {sum(1 for _ in nation_obj.pheromones.trails())}")
    console.print(f"  vocabulary:   {len(nation_obj.culture.task_catalog)} task types")
    console.print(f"  history:      {len(history)} asks")
    console.print(f"  strength:     {report.overall:.1f} / 100")


@nation.command("switch")
@click.argument("name")
def nation_switch(name: str) -> None:
    """Mark NAME as the current nation (used as the default for `anthill ask`)."""
    config = AnthillConfig.load()
    if load_nation(name, config.home) is None:
        console.print(f"[red]No nation named '{name}'.[/red]")
        raise SystemExit(1)
    _set_current_nation(config.home, name)
    console.print(f"[green]✓[/green] Current nation is now '{name}'.")


@nation.command("rename")
@click.argument("old")
@click.argument("new")
def nation_rename(old: str, new: str) -> None:
    """Rename a nation directory + its in-file name."""
    config = AnthillConfig.load()
    src = nation_dir(config.home, old)
    dst = nation_dir(config.home, new)
    if not src.exists():
        console.print(f"[red]No nation named '{old}'.[/red]")
        raise SystemExit(1)
    if dst.exists():
        console.print(f"[red]A nation named '{new}' already exists.[/red]")
        raise SystemExit(1)
    src.rename(dst)
    # Rewrite the persisted Nation under the new name so it agrees.
    nation_obj = load_nation(new, config.home)
    if nation_obj is not None:
        nation_obj.name = new
        save_nation(nation_obj, config.home)
    # Update the current pointer if it was tracking the old name.
    if _current_nation_name(config.home) == old:
        _set_current_nation(config.home, new)
    console.print(f"[green]✓[/green] Renamed '{old}' to '{new}'.")


@nation.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def nation_remove(name: str, yes: bool) -> None:
    """Delete a nation. Cannot be undone."""
    config = AnthillConfig.load()
    target = nation_dir(config.home, name)
    if not target.exists():
        console.print(f"[red]No nation named '{name}'.[/red]")
        raise SystemExit(1)
    if not yes:
        try:
            answer = input(
                f"Delete nation '{name}'? This wipes pheromones, history, "
                f"culture, everything. [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if answer not in ("y", "yes"):
            console.print("Cancelled.")
            return
    shutil.rmtree(target)
    # If removing the current one, fall back to the first remaining or
    # leave the pointer pointing at a missing dir (REPL handles).
    if _current_nation_name(config.home) == name:
        remaining = _list_nations(config.home)
        if remaining:
            _set_current_nation(config.home, remaining[0])
        else:
            pointer = _current_pointer_path(config.home)
            if pointer.exists():
                pointer.unlink()
    console.print(f"[green]✓[/green] Removed '{name}'.")
