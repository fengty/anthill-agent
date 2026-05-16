"""anthill CLI entry point.

Commands:
    anthill init [<name>]            Found a new nation
    anthill spawn --count N          Add N citizens (workers)
    anthill ask "<request>"          Hand the king's request to the nation
    anthill run "<task>" --type T    Run one typed task directly (for testing)
    anthill trails                   Show the pheromone map
    anthill identity                 Who has this nation become?
    anthill style edit               Edit the nation's house style
    anthill status                   Health overview
    anthill bench                    Compare pheromone routing vs role routing
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from anthill import __version__
from anthill.bench.compare import benchmark
from anthill.config import AnthillConfig
from anthill.core.feedback import (
    AskRecord,
    Exemplar,
    append_exemplar,
    apply_rating,
    load_exemplars,
    load_last_ask,
    save_last_ask,
)
from anthill.core.history import (
    append_history,
    build_entry_from_ask,
    find_by_id,
    load_history,
    search_history,
)
from anthill.core.costs import load_usage, summarise
from anthill.core.facts import derive_facts, read_facts, write_facts
from anthill.core.workflows import load_workflows, mine_workflows, save_workflows
from anthill.core.power import compute_ages, compute_power
from anthill.core.snapshot import export_nation, import_nation
from anthill.core.style_learner import suggest_house_style
from anthill.core.nation import Nation
from anthill.core.persistence import load_nation, nation_dir, save_nation
from anthill.core.router import RouterConfig

console = Console()


def _load_or_create(name: str, config: AnthillConfig) -> Nation:
    nation = load_nation(name, config.home)
    if nation is None:
        nation = Nation(
            name=name,
            router_config=RouterConfig(exploration=config.exploration_rate),
        )
    return nation


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="anthill")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Anthill — every user grows their own AI nation.

    Run `anthill` with no subcommand to drop into the interactive REPL.
    """
    if ctx.invoked_subcommand is None:
        from anthill.cli.repl import run_repl
        raise SystemExit(run_repl("default"))


@cli.command()
@click.argument("name", default="default")
def init(name: str) -> None:
    """Found a new nation."""
    config = AnthillConfig.load()
    config.ensure_home()
    nation = Nation(
        name=name,
        router_config=RouterConfig(exploration=config.exploration_rate),
    )
    save_nation(nation, config.home)
    console.print(f"[bold green]Nation '{name}' founded.[/bold green]")
    console.print(f"State: {config.home}/nations/{name}/")
    console.print("Add citizens with: [cyan]anthill spawn --count 5[/cyan]")


@cli.command()
@click.option("--count", default=1, help="Number of citizens to spawn.")
@click.option("--model", default=None, help="Model for new citizens (defaults to config).")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def spawn(count: int, model: str | None, nation_name: str) -> None:
    """Add new citizens to the nation."""
    config = AnthillConfig.load()
    config.ensure_home()
    nation = _load_or_create(nation_name, config)
    chosen_model = model or config.default_model
    new_agents = nation.spawn(count=count, model=chosen_model)
    save_nation(nation, config.home)
    console.print(
        f"Spawned [bold]{len(new_agents)}[/bold] citizens using [cyan]{chosen_model}[/cyan]."
    )
    console.print(f"Nation '{nation_name}' now has [bold]{len(nation.agents)}[/bold] citizens.")


@cli.command()
@click.argument("task")
@click.option("--type", "task_type", default="general", help="Task type for pheromone tracking.")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def run(task: str, task_type: str, nation_name: str) -> None:
    """Run one typed task directly. For natural-language requests, use `anthill ask`."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None or not nation.agents:
        console.print(
            f"[red]No citizens in nation '{nation_name}'.[/red] "
            f"Run [cyan]anthill spawn --count 3[/cyan] first."
        )
        return

    result = asyncio.run(nation.run(task_type, task))
    save_nation(nation, config.home)

    chosen = next((a for a in nation.agents if a.id == result.agent_id), None)
    model_name = chosen.model if chosen else "?"

    console.print(f"[dim]citizen[/dim] {result.agent_id} ({model_name})")
    console.print(f"[dim]type[/dim]    {task_type}")
    console.print(f"[dim]score[/dim]   {result.success_score:.2f}")
    console.print(f"[dim]tokens[/dim]  in={result.input_tokens} out={result.output_tokens}")
    console.print(f"[dim]took[/dim]    {result.duration_seconds:.2f}s")
    console.print()
    console.print(str(result.output))


@cli.command()
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def trails(nation_name: str) -> None:
    """Show the current pheromone map."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    table = Table(title=f"Pheromone Trails — {nation_name}")
    table.add_column("Citizen", style="cyan")
    table.add_column("Task Type", style="magenta")
    table.add_column("Strength", style="green", justify="right")

    trails_list = list(nation.pheromones.trails())
    if not trails_list:
        console.print(table)
        console.print("[dim]No trails yet. Run some tasks first.[/dim]")
        return

    trails_list.sort(key=lambda t: t.strength, reverse=True)
    for trail in trails_list:
        table.add_row(trail.agent_id, trail.task_type, f"{trail.strength:.2f}")
    console.print(table)


@cli.command()
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def status(nation_name: str) -> None:
    """Show nation status."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[bold]Anthill[/bold] — nation '{nation_name}' not founded")
        console.print(f"Run [cyan]anthill init {nation_name}[/cyan] to create it.")
        return

    trails_count = len(list(nation.pheromones.trails()))
    console.print(f"[bold]Anthill nation[/bold] — {nation_name}")
    console.print(f"  Citizens: {len(nation.agents)}")
    console.print(f"  Trails:   {trails_count}")
    console.print(f"  Home:     {config.home / 'nations' / nation_name}")


@cli.command()
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def identity(nation_name: str) -> None:
    """Show who this nation has become."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    console.print(f"[bold]Nation[/bold]    {nation_name}")
    console.print(f"[bold]Citizens[/bold]  {len(nation.agents)}")
    console.print()
    console.print("[bold]Identity[/bold]")
    console.print(f"  {nation.culture.summarize()}")
    console.print()

    if nation.culture.task_catalog:
        table = Table(title="Task vocabulary")
        table.add_column("Task type", style="magenta")
        table.add_column("Count", style="green", justify="right")
        for tt, n in sorted(nation.culture.task_catalog.items(), key=lambda x: -x[1]):
            table.add_row(tt, str(n))
        console.print(table)
        console.print()

    style = nation.culture.house_style.strip()
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
    """Inspect or edit the nation's house style."""


@style.command("edit")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def style_edit(nation_name: str) -> None:
    """Open the nation's house_style.md in $EDITOR."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    path = config.home / "nations" / nation_name / "culture" / "house_style.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# House style\n\n"
            "Write the conventions this nation should follow.\n"
            "These get injected into every citizen's system prompt.\n\n"
            "Examples:\n"
            "- Prefer terse answers. No filler.\n"
            "- Always include a working code example.\n"
            "- Default to Chinese for explanations.\n"
        )

    editor = os.getenv("EDITOR", "vi")
    subprocess.run([editor, str(path)], check=False)

    refreshed = load_nation(nation_name, config.home)
    if refreshed is not None:
        save_nation(refreshed, config.home)
    console.print(f"[green]House style saved to[/green] {path}")


@style.command("learn")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option("--model", default="deepseek-chat", help="Model used to infer style.")
def style_learn(nation_name: str, model: str) -> None:
    """Mine rated exemplars into a suggested house style. Prints; does not apply."""
    config = AnthillConfig.load()
    if load_nation(nation_name, config.home) is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    exemplars = load_exemplars(nation_dir(config.home, nation_name))
    if not exemplars:
        console.print(
            "[yellow]No rated exemplars yet. Rate a few asks with [cyan]anthill rate up/down[/cyan] first.[/yellow]"
        )
        return

    console.print(f"[dim]Reading {len(exemplars)} exemplars...[/dim]")
    suggestion = asyncio.run(suggest_house_style(exemplars, model=model))
    console.print()
    console.print("[bold]Suggested house style[/bold]")
    console.print(suggestion)
    console.print()
    console.print("[dim]Apply with [cyan]anthill style edit[/cyan] (paste in).[/dim]")


@style.command("show")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def style_show(nation_name: str) -> None:
    """Print the current house style."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    if not nation.culture.house_style.strip():
        console.print("[dim]No house style set.[/dim]")
        return
    console.print(nation.culture.house_style)


@cli.command()
@click.argument("request")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def ask(request: str, nation_name: str) -> None:
    """Hand the king's request to the nation. Scout decomposes; nation executes."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None or not nation.agents:
        console.print(
            f"[red]No citizens in nation '{nation_name}'.[/red] "
            f"Run [cyan]anthill spawn --count 3[/cyan] first."
        )
        return

    result = asyncio.run(nation.ask(request))
    save_nation(nation, config.home)

    # Record the king's most recent request so `anthill rate` has a target.
    pairs = [
        (o.final.agent_id, o.subtask.task_type)
        for o in result.outcomes
        if o.status == "ok" and o.final is not None
    ]
    if pairs:
        save_last_ask(
            AskRecord(
                request=request,
                timestamp=time.time(),
                pairs=pairs,
                final_output=result.final_output,
            ),
            nation_dir(config.home, nation_name),
        )

    # Append to permanent history — every ask gets remembered.
    append_history(
        build_entry_from_ask(request, result.plan.subtasks, result.outcomes),
        nation_dir(config.home, nation_name),
    )

    # Append per-attempt usage records for cost analysis.
    from anthill.core.costs import UsageRecord, append_usage
    for outcome in result.outcomes:
        for attempt in outcome.attempts:
            agent = next((a for a in nation.agents if a.id == attempt.agent_id), None)
            model = agent.model if agent else "unknown"
            append_usage(
                UsageRecord(
                    timestamp=time.time(),
                    agent_id=attempt.agent_id,
                    model=model,
                    task_type=attempt.task_type,
                    input_tokens=attempt.input_tokens,
                    output_tokens=attempt.output_tokens,
                ),
                nation_dir(config.home, nation_name),
            )

    console.print(f"[dim]request[/dim]  {request}")
    console.print(f"[dim]plan[/dim]     {len(result.plan)} subtask(s)")
    if len(result.plan) > 1:
        console.print()
        console.print("[bold]Plan[/bold]")
        for i, sub in enumerate(result.plan.subtasks, start=1):
            deps = (
                f" [dim](depends on: {', '.join(sub.depends_on)})[/dim]"
                if sub.depends_on
                else ""
            )
            console.print(f"  [cyan]#{i}[/cyan] [magenta]{sub.task_type}[/magenta]{deps}")

    console.print()
    last_idx = len(result.outcomes) - 1
    for i, outcome in enumerate(result.outcomes):
        sub = outcome.subtask
        is_final = i == last_idx and len(result.outcomes) > 1

        # Choose header style by status, override to green for final-and-ok.
        if outcome.status == "ok":
            header_style = "bold green" if is_final else "cyan"
        elif outcome.status == "failed":
            header_style = "bold red"
        else:
            header_style = "yellow"
        label = "Final" if (is_final and outcome.status == "ok") else f"#{i + 1}"
        status_tag = {"ok": "", "failed": " FAILED", "skipped": " SKIPPED"}[outcome.status]

        retry_note = (
            f"  [dim](after {len(outcome.attempts)} attempts)[/dim]"
            if len(outcome.attempts) > 1
            else ""
        )

        if outcome.final is not None:
            agent = next((a for a in nation.agents if a.id == outcome.final.agent_id), None)
            model = agent.model if agent else "?"
            console.print(
                f"[{header_style}]{label}{status_tag}[/{header_style}] "
                f"[magenta]{sub.task_type}[/magenta] -> {outcome.final.agent_id} ({model})  "
                f"score={outcome.final.success_score:.1f}  "
                f"{outcome.final.duration_seconds:.1f}s{retry_note}"
            )
        else:
            console.print(
                f"[{header_style}]{label}{status_tag}[/{header_style}] "
                f"[magenta]{sub.task_type}[/magenta]"
            )

        console.print(outcome.output)
        console.print()

    if not result.succeeded:
        console.print("[bold red]Request did not complete successfully.[/bold red]")
        console.print("Use [cyan]anthill trails[/cyan] to see how the failures landed in pheromones.")


@cli.group()
def workflows() -> None:
    """Inspect or mine recurring plan shapes the nation has discovered."""


@workflows.command("show")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def workflows_show(nation_name: str) -> None:
    """List the nation's stored workflow templates."""
    config = AnthillConfig.load()
    if load_nation(nation_name, config.home) is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    templates = load_workflows(nation_dir(config.home, nation_name))
    if not templates:
        console.print(
            "[dim]No workflows mined yet. Run [cyan]anthill workflows mine[/cyan].[/dim]"
        )
        return
    table = Table(title="Workflow templates")
    table.add_column("Shape", style="magenta")
    table.add_column("Runs", justify="right")
    table.add_column("Success", justify="right", style="green")
    for t in templates:
        table.add_row(t.signature, str(t.occurrences), f"{t.success_rate:.0%}")
    console.print(table)


@workflows.command("mine")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option("--min-recurrence", default=2, help="Minimum repeats to count.")
def workflows_mine(nation_name: str, min_recurrence: int) -> None:
    """Recompute workflow templates from current history."""
    config = AnthillConfig.load()
    if load_nation(nation_name, config.home) is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    history = load_history(nation_dir(config.home, nation_name))
    templates = mine_workflows(history, min_recurrence=min_recurrence)
    save_workflows(templates, nation_dir(config.home, nation_name))
    console.print(f"[green]Mined {len(templates)} workflow template(s).[/green]")


@cli.group()
def facts() -> None:
    """Inspect or refresh the nation's distilled facts."""


@facts.command("show")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def facts_show(nation_name: str) -> None:
    """Print facts.md."""
    config = AnthillConfig.load()
    if load_nation(nation_name, config.home) is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    content = read_facts(nation_dir(config.home, nation_name))
    if not content.strip():
        console.print(
            "[dim]No facts yet. Run [cyan]anthill facts refresh[/cyan] after some asks.[/dim]"
        )
        return
    console.print(content)


@facts.command("refresh")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def facts_refresh(nation_name: str) -> None:
    """Recompute deterministic facts from current history + pheromones."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return
    history = load_history(nation_dir(config.home, nation_name))
    discovered = derive_facts(history, nation.pheromones)
    write_facts(discovered, nation_dir(config.home, nation_name))
    console.print(f"[green]Wrote {len(discovered)} fact(s).[/green]")


@cli.command()
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option("--since-days", default=0, help="Only count usage from last N days (0 = all).")
def costs(nation_name: str, since_days: int) -> None:
    """Show token usage and dollar cost, aggregated by model / task / citizen."""
    import datetime

    config = AnthillConfig.load()
    if load_nation(nation_name, config.home) is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    records = load_usage(nation_dir(config.home, nation_name))
    if not records:
        console.print("[dim]No usage recorded yet. Run [cyan]anthill ask[/cyan] first.[/dim]")
        return

    since = (time.time() - since_days * 86400) if since_days > 0 else None
    report = summarise(records, since=since)

    period = ""
    if report.period_start and report.period_end:
        start = datetime.datetime.fromtimestamp(report.period_start).strftime("%m-%d %H:%M")
        end = datetime.datetime.fromtimestamp(report.period_end).strftime("%m-%d %H:%M")
        period = f"  [dim]{start} – {end}[/dim]"

    console.print(f"[bold]Costs[/bold] — {nation_name}{period}")
    console.print(
        f"  Tokens: [cyan]{report.total_input:,}[/cyan] in  +  "
        f"[cyan]{report.total_output:,}[/cyan] out"
    )
    console.print(f"  Total:  [bold green]${report.total_cost_usd:.4f}[/bold green]")
    console.print()

    def _table(title: str, items: dict[str, float]) -> None:
        if not items:
            return
        t = Table(title=title, show_header=True)
        t.add_column("Key", style="cyan")
        t.add_column("USD", style="green", justify="right")
        t.add_column("Share", style="dim", justify="right")
        for k, v in sorted(items.items(), key=lambda kv: -kv[1]):
            share = (v / report.total_cost_usd * 100) if report.total_cost_usd else 0
            t.add_row(k, f"${v:.4f}", f"{share:.1f}%")
        console.print(t)
        console.print()

    _table("By model", report.by_model)
    _table("By task type", report.by_task_type)
    _table("By citizen", report.by_agent)


@cli.command()
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def power(nation_name: str) -> None:
    """National strength — six dimensions of how capable the nation has become."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    history = load_history(nation_dir(config.home, nation_name))
    exemplars = load_exemplars(nation_dir(config.home, nation_name))
    report = compute_power(nation, history, exemplars)
    ages = compute_ages(nation, history, exemplars)

    # Visual power bar (out of 100)
    overall = report.overall
    bar_width = 30
    filled = int(round(overall / 100 * bar_width))
    bar = "█" * filled + "░" * (bar_width - filled)
    bar_color = "green" if overall >= 60 else "yellow" if overall >= 30 else "red"
    console.print(f"[bold]{nation_name}[/bold]  national strength")
    console.print(f"[{bar_color}]{bar}[/{bar_color}]  {overall:.1f} / 100")
    console.print()

    table = Table(show_header=False, box=None)
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="bold")
    table.add_column("Meaning", style="dim")

    table.add_row("Vocabulary", str(report.vocabulary), "distinct task types ever handled")
    table.add_row("Specialists", str(report.specialists), "citizens with strong trails")
    table.add_row(
        "Success rate",
        f"{report.success_rate:.1%}",
        f"{report.total_tasks} subtasks across {report.total_asks} asks",
    )
    table.add_row("Max chain", str(report.max_chain), "longest successful multi-step plan")
    table.add_row(
        "Feedback",
        f"{report.feedback_score:+d}",
        "net rating volume (ups minus downs)",
    )
    table.add_row(
        "Diversity",
        f"{report.diversity:.2f}",
        "how work spreads across citizens (0=concentrated, 1=spread)",
    )
    console.print(table)
    console.print()

    # The four ages — progression timeline.
    console.print("[bold]The four ages[/bold]")
    for age in ages:
        icon = "[bold green]✓[/bold green]" if age.completed else "[dim]·[/dim]"
        mini_bar_width = 12
        filled = int(round(age.progress * mini_bar_width))
        mini_bar = "█" * filled + "░" * (mini_bar_width - filled)
        color = "green" if age.completed else "yellow"
        console.print(
            f"  {icon} [{color}]{age.name:<14}[/{color}] "
            f"[{color}]{mini_bar}[/{color}]  [dim]{age.description}[/dim]"
        )


@cli.group()
def history() -> None:
    """Browse the nation's permanent record of past asks."""


@history.command("list")
@click.option("--limit", default=20, help="Number of recent entries to show.")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def history_list(limit: int, nation_name: str) -> None:
    """List recent asks (id, timestamp, request, status)."""
    config = AnthillConfig.load()
    entries = load_history(nation_dir(config.home, nation_name), limit=limit)
    if not entries:
        console.print("[dim]No history yet.[/dim]")
        return

    import datetime

    table = Table(title=f"History — {nation_name}")
    table.add_column("ID", style="cyan")
    table.add_column("When", style="dim")
    table.add_column("Status", justify="center")
    table.add_column("Request")

    for e in entries:
        when = datetime.datetime.fromtimestamp(e.timestamp).strftime("%m-%d %H:%M")
        statuses = [o["status"] for o in e.outcomes]
        if all(s == "ok" for s in statuses):
            status = "[green]ok[/green]"
        elif any(s == "failed" for s in statuses):
            status = "[red]failed[/red]"
        else:
            status = "[yellow]partial[/yellow]"
        request_preview = e.request if len(e.request) <= 60 else e.request[:57] + "..."
        table.add_row(e.id, when, status, request_preview)

    console.print(table)


@history.command("show")
@click.argument("entry_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def history_show(entry_id: str, nation_name: str) -> None:
    """Show full detail of one past ask."""
    config = AnthillConfig.load()
    entry = find_by_id(entry_id, nation_dir(config.home, nation_name))
    if entry is None:
        console.print(f"[red]No entry matching '{entry_id}'.[/red]")
        return

    import datetime
    when = datetime.datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M:%S")
    console.print(f"[bold]Entry[/bold] {entry.id}  [dim]({when})[/dim]")
    console.print(f"[bold]Request[/bold] {entry.request}")
    console.print()
    for i, outcome in enumerate(entry.outcomes, start=1):
        color = {"ok": "green", "failed": "red", "skipped": "yellow"}[outcome["status"]]
        console.print(
            f"[cyan]#{i}[/cyan] [magenta]{outcome['task_type']}[/magenta] "
            f"[{color}]{outcome['status']}[/{color}]  "
            f"(attempts: {outcome['attempts']})"
        )
        if outcome.get("final_output"):
            console.print(f"  {outcome['final_output']}")
        elif outcome.get("skip_reason"):
            console.print(f"  [dim]{outcome['skip_reason']}[/dim]")
        console.print()


@history.command("search")
@click.argument("query")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def history_search(query: str, nation_name: str) -> None:
    """Search past asks by substring of the request."""
    config = AnthillConfig.load()
    matches = search_history(query, nation_dir(config.home, nation_name))
    if not matches:
        console.print(f"[dim]No history matches '{query}'.[/dim]")
        return
    for e in matches:
        console.print(f"[cyan]{e.id}[/cyan]  {e.request}")


@cli.command("export")
@click.option("--nation", "nation_name", default="default", help="Nation to export.")
@click.argument("output", type=click.Path(path_type=Path))
def export_cmd(nation_name: str, output: Path) -> None:
    """Bundle a nation into a .tar.gz snapshot."""
    config = AnthillConfig.load()
    src = nation_dir(config.home, nation_name)
    if not src.exists():
        console.print(f"[red]Nation '{nation_name}' not found.[/red]")
        return
    if output.suffix == "":
        output = output.with_suffix(".tar.gz")
    manifest = export_nation(src, output)
    console.print(f"[green]Exported[/green] '{nation_name}' to {output}")
    console.print(
        f"  citizens={manifest.citizen_count}  "
        f"vocabulary={manifest.vocabulary_size}  "
        f"history={manifest.history_entries}  "
        f"version={manifest.anthill_version}"
    )


@cli.command("import")
@click.argument("archive", type=click.Path(exists=True, path_type=Path))
def import_cmd(archive: Path) -> None:
    """Restore a nation from a .tar.gz snapshot."""
    config = AnthillConfig.load()
    config.ensure_home()
    target_root = config.home / "nations"
    try:
        manifest = import_nation(archive, target_root)
    except FileExistsError as e:
        console.print(f"[red]{e}[/red]")
        return
    console.print(
        f"[green]Imported[/green] nation '{manifest.nation_name}' from {archive}"
    )
    console.print(
        f"  citizens={manifest.citizen_count}  "
        f"vocabulary={manifest.vocabulary_size}  "
        f"history={manifest.history_entries}  "
        f"version={manifest.anthill_version}"
    )


@cli.command()
@click.argument("verdict", type=click.Choice(["up", "down"]))
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option("--weight", default=2.0, help="Rating impact magnitude.")
def rate(verdict: str, nation_name: str, weight: float) -> None:
    """Rate the last `anthill ask` up or down. Reshapes pheromone trails."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    record = load_last_ask(nation_dir(config.home, nation_name))
    if record is None:
        console.print("[yellow]No recent ask to rate. Run [cyan]anthill ask[/cyan] first.[/yellow]")
        return

    touched = apply_rating(verdict, record, nation.pheromones, weight=weight)
    save_nation(nation, config.home)

    # Preserve the rated output as an exemplar for future style learning.
    if record.final_output:
        append_exemplar(
            Exemplar(
                rating=verdict,
                request=record.request,
                output=record.final_output,
                timestamp=time.time(),
            ),
            nation_dir(config.home, nation_name),
        )

    icon = "+" if verdict == "up" else "-"
    color = "green" if verdict == "up" else "red"
    console.print(
        f"[bold {color}]{icon} Rating '{verdict}' applied[/bold {color}] "
        f"to {touched} trail(s) for request:"
    )
    console.print(f"  [dim]{record.request}[/dim]")


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
