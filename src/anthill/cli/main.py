"""anthill CLI entry point.

Commands:
    anthill init <name>         Initialize a colony
    anthill spawn --count N     Add N workers
    anthill run "<task>"        Give the colony a task
    anthill trails              Show current pheromone map
    anthill status              Colony health overview
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from anthill import __version__
from anthill.core.colony import Colony

console = Console()


@click.group()
@click.version_option(__version__, prog_name="anthill")
def cli() -> None:
    """Anthill — a colony of agents that organize themselves."""


@cli.command()
@click.argument("name", default="default")
def init(name: str) -> None:
    """Initialize a new colony."""
    console.print(f"[bold green]Colony '{name}' initialized.[/bold green]")
    console.print("Spawn workers with: [cyan]anthill spawn --count 5[/cyan]")


@cli.command()
@click.option("--count", default=1, help="Number of workers to spawn.")
@click.option("--model", default="claude-sonnet-4-5", help="Model for new workers.")
def spawn(count: int, model: str) -> None:
    """Add new workers to the colony."""
    colony = Colony()
    agents = colony.spawn(count=count, model=model)
    console.print(f"Spawned [bold]{len(agents)}[/bold] workers using {model}.")


@cli.command()
@click.argument("task")
@click.option("--type", "task_type", default="general", help="Task type for pheromone tracking.")
def run(task: str, task_type: str) -> None:
    """Give the colony a task."""
    console.print(f"[dim]Task type:[/dim] {task_type}")
    console.print(f"[dim]Task:[/dim] {task}")
    console.print("[yellow]Execution layer pending — v0.0.2[/yellow]")


@cli.command()
def trails() -> None:
    """Show the current pheromone map."""
    table = Table(title="Pheromone Trails")
    table.add_column("Agent", style="cyan")
    table.add_column("Task Type", style="magenta")
    table.add_column("Strength", style="green", justify="right")

    # placeholder — real implementation reads from persisted colony state
    console.print(table)
    console.print("[dim]No trails yet. Run some tasks first.[/dim]")


@cli.command()
def status() -> None:
    """Show colony status."""
    console.print("[bold]Anthill colony status[/bold]")
    console.print("  Workers: 0")
    console.print("  Trails:  0")
    console.print("  State:   [yellow]idle[/yellow]")


if __name__ == "__main__":
    cli()
