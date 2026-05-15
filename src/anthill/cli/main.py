"""anthill CLI entry point.

Commands:
    anthill init [<name>]            Initialize a colony
    anthill spawn --count N          Add N workers
    anthill run "<task>"             Give the colony a task (calls real model)
    anthill trails                   Show current pheromone map
    anthill status                   Colony health overview
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import click
from rich.console import Console
from rich.table import Table

from anthill import __version__
from anthill.bench.compare import benchmark
from anthill.config import AnthillConfig
from anthill.core.colony import Colony
from anthill.core.persistence import load_colony, save_colony
from anthill.core.router import RouterConfig

console = Console()


def _load_or_create(name: str, config: AnthillConfig) -> Colony:
    colony = load_colony(name, config.home)
    if colony is None:
        colony = Colony(
            name=name,
            router_config=RouterConfig(exploration=config.exploration_rate),
        )
    return colony


@click.group()
@click.version_option(__version__, prog_name="anthill")
def cli() -> None:
    """Anthill — a colony of agents that organize themselves."""


@cli.command()
@click.argument("name", default="default")
def init(name: str) -> None:
    """Initialize a new colony."""
    config = AnthillConfig.load()
    config.ensure_home()
    colony = Colony(
        name=name,
        router_config=RouterConfig(exploration=config.exploration_rate),
    )
    save_colony(colony, config.home)
    console.print(f"[bold green]Colony '{name}' initialized.[/bold green]")
    console.print(f"State: {config.home}/colonies/{name}/")
    console.print("Spawn workers with: [cyan]anthill spawn --count 5[/cyan]")


@cli.command()
@click.option("--count", default=1, help="Number of workers to spawn.")
@click.option("--model", default=None, help="Model for new workers (defaults to config).")
@click.option("--colony", "colony_name", default="default", help="Colony name.")
def spawn(count: int, model: str | None, colony_name: str) -> None:
    """Add new workers to the colony."""
    config = AnthillConfig.load()
    config.ensure_home()
    colony = _load_or_create(colony_name, config)
    chosen_model = model or config.default_model
    new_agents = colony.spawn(count=count, model=chosen_model)
    save_colony(colony, config.home)
    console.print(f"Spawned [bold]{len(new_agents)}[/bold] workers using [cyan]{chosen_model}[/cyan].")
    console.print(f"Colony '{colony_name}' now has [bold]{len(colony.agents)}[/bold] agents.")


@cli.command()
@click.argument("task")
@click.option("--type", "task_type", default="general", help="Task type for pheromone tracking.")
@click.option("--colony", "colony_name", default="default", help="Colony name.")
def run(task: str, task_type: str, colony_name: str) -> None:
    """Give the colony a task."""
    config = AnthillConfig.load()
    colony = load_colony(colony_name, config.home)
    if colony is None or not colony.agents:
        console.print(
            f"[red]No agents in colony '{colony_name}'.[/red] "
            f"Run [cyan]anthill spawn --count 3[/cyan] first."
        )
        return

    result = asyncio.run(colony.run(task_type, task))
    save_colony(colony, config.home)

    chosen = next((a for a in colony.agents if a.id == result.agent_id), None)
    model_name = chosen.model if chosen else "?"

    console.print(f"[dim]agent[/dim]   {result.agent_id} ({model_name})")
    console.print(f"[dim]type[/dim]    {task_type}")
    console.print(f"[dim]score[/dim]   {result.success_score:.2f}")
    console.print(f"[dim]tokens[/dim]  in={result.input_tokens} out={result.output_tokens}")
    console.print(f"[dim]took[/dim]    {result.duration_seconds:.2f}s")
    console.print()
    console.print(str(result.output))


@cli.command()
@click.option("--colony", "colony_name", default="default", help="Colony name.")
def trails(colony_name: str) -> None:
    """Show the current pheromone map."""
    config = AnthillConfig.load()
    colony = load_colony(colony_name, config.home)
    if colony is None:
        console.print(f"[red]No colony named '{colony_name}'.[/red]")
        return

    table = Table(title=f"Pheromone Trails — {colony_name}")
    table.add_column("Agent", style="cyan")
    table.add_column("Task Type", style="magenta")
    table.add_column("Strength", style="green", justify="right")

    trails_list = list(colony.pheromones.trails())
    if not trails_list:
        console.print(table)
        console.print("[dim]No trails yet. Run some tasks first.[/dim]")
        return

    trails_list.sort(key=lambda t: t.strength, reverse=True)
    for trail in trails_list:
        table.add_row(trail.agent_id, trail.task_type, f"{trail.strength:.2f}")
    console.print(table)


@cli.command()
@click.option("--colony", "colony_name", default="default", help="Colony name.")
def status(colony_name: str) -> None:
    """Show colony status."""
    config = AnthillConfig.load()
    colony = load_colony(colony_name, config.home)
    if colony is None:
        console.print(f"[bold]Anthill[/bold] — colony '{colony_name}' not initialized")
        console.print(f"Run [cyan]anthill init {colony_name}[/cyan] to create it.")
        return

    trails_count = len(list(colony.pheromones.trails()))
    console.print(f"[bold]Anthill colony[/bold] — {colony_name}")
    console.print(f"  Workers: {len(colony.agents)}")
    console.print(f"  Trails:  {trails_count}")
    console.print(f"  Home:    {config.home / 'colonies' / colony_name}")


@cli.command()
@click.option("--colony", "colony_name", default="default", help="Colony name.")
def identity(colony_name: str) -> None:
    """Show who this colony has become."""
    config = AnthillConfig.load()
    colony = load_colony(colony_name, config.home)
    if colony is None:
        console.print(f"[red]No colony named '{colony_name}'.[/red]")
        return

    console.print(f"[bold]Colony[/bold]   {colony_name}")
    console.print(f"[bold]Workers[/bold]  {len(colony.agents)}")
    console.print()
    console.print("[bold]Identity[/bold]")
    console.print(f"  {colony.culture.summarize()}")
    console.print()

    if colony.culture.task_catalog:
        table = Table(title="Task vocabulary")
        table.add_column("Task type", style="magenta")
        table.add_column("Count", style="green", justify="right")
        for tt, n in sorted(colony.culture.task_catalog.items(), key=lambda x: -x[1]):
            table.add_row(tt, str(n))
        console.print(table)
        console.print()

    style = colony.culture.house_style.strip()
    if style:
        console.print("[bold]House style[/bold]")
        for line in style.splitlines():
            console.print(f"  {line}")
    else:
        console.print(
            "[dim]No house style set. "
            "Edit it with [cyan]anthill style edit[/cyan].[/dim]"
        )


@cli.group()
def style() -> None:
    """Inspect or edit the colony's house style."""


@style.command("edit")
@click.option("--colony", "colony_name", default="default", help="Colony name.")
def style_edit(colony_name: str) -> None:
    """Open the colony's house_style.md in $EDITOR."""
    config = AnthillConfig.load()
    colony = load_colony(colony_name, config.home)
    if colony is None:
        console.print(f"[red]No colony named '{colony_name}'.[/red]")
        return

    path = config.home / "colonies" / colony_name / "culture" / "house_style.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# House style\n\n"
            "Write the conventions this colony should follow.\n"
            "These get injected into every worker's system prompt.\n\n"
            "Examples:\n"
            "- Prefer terse answers. No filler.\n"
            "- Always include a working code example.\n"
            "- Default to Chinese for explanations.\n"
        )

    editor = os.getenv("EDITOR", "vi")
    subprocess.run([editor, str(path)], check=False)

    refreshed = load_colony(colony_name, config.home)
    if refreshed is not None:
        save_colony(refreshed, config.home)
    console.print(f"[green]House style saved to[/green] {path}")


@style.command("show")
@click.option("--colony", "colony_name", default="default", help="Colony name.")
def style_show(colony_name: str) -> None:
    """Print the current house style."""
    config = AnthillConfig.load()
    colony = load_colony(colony_name, config.home)
    if colony is None:
        console.print(f"[red]No colony named '{colony_name}'.[/red]")
        return
    if not colony.culture.house_style.strip():
        console.print("[dim]No house style set.[/dim]")
        return
    console.print(colony.culture.house_style)


@cli.command()
@click.argument("request")
@click.option("--colony", "colony_name", default="default", help="Colony name.")
def ask(request: str, colony_name: str) -> None:
    """Hand the colony a natural-language request. Scout decomposes; colony executes."""
    config = AnthillConfig.load()
    colony = load_colony(colony_name, config.home)
    if colony is None or not colony.agents:
        console.print(
            f"[red]No agents in colony '{colony_name}'.[/red] "
            f"Run [cyan]anthill spawn --count 3[/cyan] first."
        )
        return

    result = asyncio.run(colony.ask(request))
    save_colony(colony, config.home)

    console.print(f"[dim]request[/dim]  {request}")
    console.print(f"[dim]plan[/dim]     {len(result.plan)} subtask(s)")
    console.print()
    for i, (sub, res) in enumerate(zip(result.plan.subtasks, result.results), start=1):
        agent = next((a for a in colony.agents if a.id == res.agent_id), None)
        model = agent.model if agent else "?"
        console.print(
            f"[cyan]#{i}[/cyan] "
            f"[magenta]{sub.task_type}[/magenta] -> {res.agent_id} ({model})  "
            f"score={res.success_score:.1f}  {res.duration_seconds:.1f}s"
        )
        console.print(str(res.output))
        console.print()


@cli.command()
@click.option("--terse-tasks", default=25, help="Terse tasks per arm.")
@click.option("--verbose-tasks", default=25, help="Verbose tasks per arm.")
@click.option("--model", default="deepseek-chat", help="Model used by all agents.")
@click.option("--exploration", default=0.10, help="Pheromone exploration rate.")
@click.option("--seed", default=42, help="Seed for task pool and role pick.")
def bench(
    terse_tasks: int,
    verbose_tasks: int,
    model: str,
    exploration: float,
    seed: int,
) -> None:
    """Compare role routing vs pheromone routing.

    The central experiment of this project. Both arms get the same agents
    and the same tasks; only the routing differs.
    """
    console.print(f"[dim]Running benchmark — {terse_tasks + verbose_tasks} tasks per arm[/dim]")
    result = asyncio.run(
        benchmark(
            n_terse_tasks=terse_tasks,
            n_verbose_tasks=verbose_tasks,
            model=model,
            exploration=exploration,
            seed=seed,
        )
    )
    console.print()
    console.print(result.summary())
    console.print()
    if result.gap > 0.05:
        console.print(f"[bold green]Pheromone wins by {result.gap:.1%}.[/bold green]")
    elif result.gap < -0.05:
        console.print(f"[bold red]Role routing wins by {-result.gap:.1%}.[/bold red]")
    else:
        console.print("[bold yellow]Too close to call.[/bold yellow]")


if __name__ == "__main__":
    cli()
