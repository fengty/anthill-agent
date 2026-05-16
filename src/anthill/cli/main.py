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
from anthill.core.background import (
    cancel_job,
    clear_job,
    list_jobs,
    load_job,
    read_log,
    start_background,
)
from anthill.core.budget import Budget
from anthill.core.inflight import (
    clear_inflight,
    list_inflight,
    load_inflight,
)
from anthill.core.recipes import (
    Recipe,
    list_recipes,
    load_recipe,
    record_run,
    remove_recipe,
    save_recipe,
)
from anthill.core.costs import load_usage, summarise
from anthill.core.facts import derive_facts, read_facts, write_facts
from anthill.core.workflows import load_workflows, mine_workflows, save_workflows
from anthill.plugins import default_registry
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
@click.option("--force", is_flag=True, help="Run even if models are already configured.")
def setup(force: bool) -> None:
    """First-run wizard: pick a model, found a nation, optionally add an IM channel."""
    from anthill.cli.setup import run_wizard
    raise SystemExit(run_wizard(force=force))


# 'anthill model ...' subcommand group.
from anthill.cli.model_cmd import model as _model_group  # noqa: E402

cli.add_command(_model_group)


@cli.command()
def doctor() -> None:
    """Run a full self-check and print a status report."""
    from anthill.cli.doctor import run_doctor
    raise SystemExit(run_doctor())


# 'anthill nation ...' subcommand group.
from anthill.cli.nation_cmd import nation as _nation_group  # noqa: E402

cli.add_command(_nation_group)


# 'anthill channel ...' subcommand group.
from anthill.cli.channel_cmd import channel as _channel_group  # noqa: E402

cli.add_command(_channel_group)


# 'anthill citizen ...' subcommand group.
from anthill.cli.citizen_cmd import citizen as _citizen_group  # noqa: E402

cli.add_command(_citizen_group)


# 'anthill values ...' subcommand group (v0.4 — open-vocabulary dimensions).
from anthill.cli.values_cmd import values as _values_group  # noqa: E402

cli.add_command(_values_group)


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
            "- Default to plain prose, no markdown.\n"
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


def _finalize_ask(nation: Nation, nation_name: str, config: AnthillConfig, result, request: str) -> None:
    """Persist + render the outcome of an ask. Shared by `ask` and `resume`.

    Both entry points need the same downstream effects — write the
    last-ask record so `rate` has a target, append history, log usage,
    and print the per-subtask result block. Keeping this in one place
    means resume can never silently drift from ask in what gets stored.
    """
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
    if result.replans:
        console.print(
            f"[dim]replan[/dim]   [yellow]{result.replans}[/yellow] self-correction pass(es) applied"
        )
    if result.ask_id:
        console.print(f"[dim]ask_id[/dim]   {result.ask_id}")
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

    if result.budget is not None:
        if result.budget.exhausted:
            console.print(
                f"[bold yellow]Budget exhausted ({result.budget.exhausted}).[/bold yellow] "
                f"[dim]{result.budget.summary}[/dim]"
            )
        else:
            console.print(f"[dim]budget[/dim]  {result.budget.summary}")

    if not result.succeeded:
        console.print("[bold red]Request did not complete successfully.[/bold red]")
        console.print(
            "Use [cyan]anthill trails[/cyan] to see how the failures landed in pheromones."
        )
        if result.ask_id:
            console.print(
                f"Resume with [cyan]anthill resume {result.ask_id}[/cyan]."
            )


@cli.command()
@click.argument("request")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option(
    "--max-tokens",
    type=int,
    default=None,
    help="Stop the ask once cumulative input+output tokens exceed this cap.",
)
@click.option(
    "--max-cost",
    type=float,
    default=None,
    help="Stop the ask once estimated spend exceeds this many USD.",
)
@click.option(
    "--max-seconds",
    type=float,
    default=None,
    help="Stop the ask once it has been running this many seconds.",
)
@click.option(
    "--max-replans",
    type=int,
    default=1,
    help="How many self-correction passes Scout may run when a subtask fails. 0 disables.",
)
@click.option(
    "--ensemble",
    type=int,
    default=1,
    help="v0.6 — run every subtask K times in parallel on distinct citizens.",
)
@click.option(
    "--strategy",
    default="first_success",
    help="Winner selection when --ensemble > 1: first_success / highest_score / shortest_correct / majority",
)
def ask(
    request: str,
    nation_name: str,
    max_tokens: int | None,
    max_cost: float | None,
    max_seconds: float | None,
    max_replans: int,
    ensemble: int,
    strategy: str,
) -> None:
    """Hand the king's request to the nation. Scout decomposes; nation executes."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None or not nation.agents:
        console.print(
            f"[red]No citizens in nation '{nation_name}'.[/red] "
            f"Run [cyan]anthill spawn --count 3[/cyan] first."
        )
        return

    budget = Budget(
        max_tokens=max_tokens,
        max_cost_usd=max_cost,
        max_seconds=max_seconds,
    )
    async def _run():
        result = await nation.ask(
            request,
            nation_dir=nation_dir(config.home, nation_name),
            budget=budget if not budget.is_empty() else None,
            max_replans=max_replans,
        )
        # Apply the CLI-level ensemble override AFTER planning but before
        # execution? We can't — nation.ask already ran. So instead we
        # stamp the plan's subtasks before passing through. Simpler path:
        # mutate plan subtasks in the AskResult and surface them is wrong;
        # we want the plan to be built with fanout already set. Re-do as
        # pre_plan approach below.
        return result

    # If user asked for ensemble, we need fanout set BEFORE plan executes.
    # Do this by intercepting the plan: pre-plan via Scout, stamp fanout
    # on every subtask, then pass pre_plan to nation.ask.
    if ensemble > 1:
        from anthill.core.ensemble import known_strategies
        if strategy not in known_strategies():
            console.print(
                f"[red]Unknown --strategy '{strategy}'.[/red] "
                f"Available: {', '.join(known_strategies())}"
            )
            return
        from anthill.core.scout import Scout

        async def _ensemble_run():
            scout = Scout(model=nation.scout_model)
            plan = await scout.plan(
                request,
                known_task_types=nation.culture.known_task_types(),
            )
            for st in plan.subtasks:
                st.fanout = ensemble
                st.strategy = strategy
            return await nation.ask(
                request,
                nation_dir=nation_dir(config.home, nation_name),
                budget=budget if not budget.is_empty() else None,
                max_replans=max_replans,
                pre_plan=plan,
            )

        result = asyncio.run(_ensemble_run())
    else:
        result = asyncio.run(_run())
    _finalize_ask(nation, nation_name, config, result, request)


@cli.command("resume")
@click.argument("ask_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def resume_cmd(ask_id: str, nation_name: str) -> None:
    """Resume an interrupted ask from its inflight checkpoint."""
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None or not nation.agents:
        console.print(
            f"[red]No citizens in nation '{nation_name}'.[/red] "
            f"Run [cyan]anthill spawn --count 3[/cyan] first."
        )
        return

    inflight = load_inflight(ask_id, nation_dir(config.home, nation_name))
    if inflight is None:
        console.print(f"[red]No inflight ask matching '{ask_id}'.[/red]")
        console.print("List with [cyan]anthill inflight list[/cyan].")
        return

    done = len(inflight.completed)
    total = len(inflight.plan.subtasks)
    console.print(
        f"[dim]resuming[/dim] {inflight.ask_id}  "
        f"[dim]({done}/{total} subtasks already done)[/dim]"
    )
    result = asyncio.run(
        nation.ask(
            inflight.request,
            resume=inflight,
            nation_dir=nation_dir(config.home, nation_name),
        )
    )
    _finalize_ask(nation, nation_name, config, result, inflight.request)


@cli.group()
def inflight() -> None:
    """Inspect and manage in-flight (interrupted) asks."""


@inflight.command("list")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def inflight_list(nation_name: str) -> None:
    """List all in-flight checkpoints for the nation."""
    config = AnthillConfig.load()
    asks = list_inflight(nation_dir(config.home, nation_name))
    if not asks:
        console.print("[dim]No in-flight asks. All clean.[/dim]")
        return

    import datetime

    table = Table(title=f"In-flight asks — {nation_name}")
    table.add_column("ID", style="cyan")
    table.add_column("Started", style="dim")
    table.add_column("Done", justify="right")
    table.add_column("Request")
    for a in asks:
        when = datetime.datetime.fromtimestamp(a.started_at).strftime("%m-%d %H:%M")
        total = len(a.plan.subtasks)
        done = len(a.completed)
        preview = a.request if len(a.request) <= 60 else a.request[:57] + "..."
        table.add_row(a.ask_id, when, f"{done}/{total}", preview)
    console.print(table)


@inflight.command("show")
@click.argument("ask_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def inflight_show(ask_id: str, nation_name: str) -> None:
    """Show one in-flight ask: plan + which subtasks have completed."""
    config = AnthillConfig.load()
    inf = load_inflight(ask_id, nation_dir(config.home, nation_name))
    if inf is None:
        console.print(f"[red]No in-flight ask matching '{ask_id}'.[/red]")
        return

    import datetime
    when = datetime.datetime.fromtimestamp(inf.started_at).strftime("%Y-%m-%d %H:%M:%S")
    console.print(f"[bold]Ask[/bold] {inf.ask_id}  [dim]({when})[/dim]")
    console.print(f"[bold]Request[/bold] {inf.request}")
    console.print()
    done = inf.completed_indices()
    for i, sub in enumerate(inf.plan.subtasks):
        if i in done:
            icon = "[green]✓[/green]"
        else:
            icon = "[dim]·[/dim]"
        deps = (
            f" [dim](depends on: {', '.join(sub.depends_on)})[/dim]"
            if sub.depends_on
            else ""
        )
        console.print(f"  {icon} [cyan]#{i + 1}[/cyan] [magenta]{sub.task_type}[/magenta]{deps}")
    console.print()
    console.print(f"Resume with [cyan]anthill resume {inf.ask_id}[/cyan].")


@inflight.command("clear")
@click.argument("ask_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def inflight_clear(ask_id: str, nation_name: str) -> None:
    """Drop an in-flight checkpoint without resuming it."""
    config = AnthillConfig.load()
    if clear_inflight(ask_id, nation_dir(config.home, nation_name)):
        console.print(f"[green]Cleared[/green] in-flight ask {ask_id}.")
    else:
        console.print(f"[red]No in-flight ask matching '{ask_id}'.[/red]")


@cli.group()
def recipe() -> None:
    """Save, list, and replay parameterized request templates."""


@recipe.command("save")
@click.argument("name")
@click.argument("template")
@click.option("--desc", "description", default="", help="Short human-readable note.")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def recipe_save(name: str, template: str, description: str, nation_name: str) -> None:
    """Save a request template under a name.

    Use {placeholders} for slots that change per run:

        anthill recipe save brief "Research {topic} and write a one-page brief."
        anthill recipe run brief --arg topic="quantum computing"
    """
    config = AnthillConfig.load()
    target_dir = nation_dir(config.home, nation_name)
    r = Recipe(name=name, template=template, description=description)
    save_recipe(r, target_dir)
    placeholders = r.placeholders()
    if placeholders:
        console.print(
            f"[green]Saved[/green] recipe [cyan]{r.name}[/cyan]  "
            f"[dim](placeholders: {', '.join(placeholders)})[/dim]"
        )
    else:
        console.print(
            f"[green]Saved[/green] recipe [cyan]{r.name}[/cyan]  "
            f"[dim](no placeholders)[/dim]"
        )


@recipe.command("list")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def recipe_list(nation_name: str) -> None:
    """Show every saved recipe."""
    config = AnthillConfig.load()
    items = list_recipes(nation_dir(config.home, nation_name))
    if not items:
        console.print("[dim]No recipes yet.[/dim]")
        return
    table = Table(title=f"Recipes — {nation_name}")
    table.add_column("Name", style="cyan")
    table.add_column("Placeholders", style="magenta")
    table.add_column("Runs", justify="right")
    table.add_column("Description", style="dim")
    for r in items:
        ph = ", ".join(r.placeholders()) or "—"
        table.add_row(r.name, ph, str(r.run_count), r.description[:60])
    console.print(table)


@recipe.command("show")
@click.argument("name")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def recipe_show(name: str, nation_name: str) -> None:
    """Print one recipe's full template and metadata."""
    config = AnthillConfig.load()
    r = load_recipe(name, nation_dir(config.home, nation_name))
    if r is None:
        console.print(f"[red]No recipe named '{name}'.[/red]")
        return
    console.print(f"[bold]Recipe[/bold] {r.name}")
    if r.description:
        console.print(f"[dim]{r.description}[/dim]")
    console.print()
    console.print(f"[bold]Template[/bold]\n  {r.template}")
    ph = r.placeholders()
    if ph:
        console.print(f"\n[bold]Placeholders[/bold]  {', '.join(ph)}")
    if r.subtasks:
        console.print("\n[bold]Explicit subtasks[/bold]")
        for i, s in enumerate(r.subtasks, start=1):
            deps = (
                f" [dim](depends on: {', '.join(s.depends_on)})[/dim]"
                if s.depends_on
                else ""
            )
            console.print(f"  [cyan]#{i}[/cyan] [magenta]{s.task_type}[/magenta]{deps}")
            console.print(f"     {s.prompt_template}")
    console.print()
    console.print(f"[dim]runs: {r.run_count}[/dim]")


@recipe.command("remove")
@click.argument("name")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def recipe_remove(name: str, nation_name: str) -> None:
    """Delete a saved recipe."""
    config = AnthillConfig.load()
    if remove_recipe(name, nation_dir(config.home, nation_name)):
        console.print(f"[green]Removed[/green] {name}.")
    else:
        console.print(f"[red]No recipe named '{name}'.[/red]")


@recipe.command("run")
@click.argument("name")
@click.option(
    "--arg",
    "args_raw",
    multiple=True,
    help="key=value placeholder substitution (repeatable).",
)
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option(
    "--max-tokens", type=int, default=None,
    help="Per-ask token cap (see `anthill ask --max-tokens`).",
)
@click.option(
    "--max-cost", type=float, default=None,
    help="Per-ask USD cap (see `anthill ask --max-cost`).",
)
@click.option(
    "--max-seconds", type=float, default=None,
    help="Per-ask wall-clock cap (see `anthill ask --max-seconds`).",
)
def recipe_run(
    name: str,
    args_raw: tuple[str, ...],
    nation_name: str,
    max_tokens: int | None,
    max_cost: float | None,
    max_seconds: float | None,
) -> None:
    """Execute a saved recipe with key=value substitutions."""
    config = AnthillConfig.load()
    target_dir = nation_dir(config.home, nation_name)
    r = load_recipe(name, target_dir)
    if r is None:
        console.print(f"[red]No recipe named '{name}'.[/red]")
        return

    args: dict[str, str] = {}
    for raw in args_raw:
        if "=" not in raw:
            console.print(f"[yellow]Skipping malformed arg: {raw!r}[/yellow]")
            continue
        k, v = raw.split("=", 1)
        args[k.strip()] = v

    try:
        filled = r.fill(args)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        return

    nation = load_nation(nation_name, config.home)
    if nation is None or not nation.agents:
        console.print(
            f"[red]No citizens in nation '{nation_name}'.[/red] "
            f"Run [cyan]anthill spawn --count 3[/cyan] first."
        )
        return

    budget = Budget(
        max_tokens=max_tokens,
        max_cost_usd=max_cost,
        max_seconds=max_seconds,
    )
    result = asyncio.run(
        nation.ask(
            filled.request,
            nation_dir=target_dir,
            budget=budget if not budget.is_empty() else None,
            pre_plan=filled.plan,
        )
    )
    record_run(r, target_dir)
    _finalize_ask(nation, nation_name, config, result, filled.request)


@cli.group()
def bg() -> None:
    """Run asks detached in the background; check status later."""


@bg.command("ask")
@click.argument("request")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def bg_ask(request: str, nation_name: str) -> None:
    """Spawn an ask in the background. Returns immediately with a job_id."""
    config = AnthillConfig.load()
    target_dir = nation_dir(config.home, nation_name)
    target_dir.mkdir(parents=True, exist_ok=True)

    job = start_background(request, nation_name, target_dir)
    console.print(
        f"[green]Started[/green] background ask "
        f"[cyan]{job.job_id}[/cyan]  [dim](pid {job.pid})[/dim]"
    )
    console.print(f"  Tail with [cyan]anthill bg show {job.job_id}[/cyan].")


@bg.command("list")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def bg_list(nation_name: str) -> None:
    """Show every background job for the nation."""
    config = AnthillConfig.load()
    jobs = list_jobs(nation_dir(config.home, nation_name))
    if not jobs:
        console.print("[dim]No background jobs.[/dim]")
        return

    table = Table(title=f"Background jobs — {nation_name}")
    table.add_column("ID", style="cyan")
    table.add_column("Started", style="dim")
    table.add_column("Status")
    table.add_column("Runtime", justify="right")
    table.add_column("Request")
    color = {
        "running": "yellow",
        "completed": "green",
        "failed": "red",
        "died": "red",
        "cancelled": "dim",
    }
    for j in jobs:
        st = j.status
        preview = j.request if len(j.request) <= 50 else j.request[:47] + "..."
        table.add_row(
            j.job_id,
            j.started_at_human(),
            f"[{color[st]}]{st}[/{color[st]}]",
            f"{j.runtime_seconds:.1f}s",
            preview,
        )
    console.print(table)


@bg.command("show")
@click.argument("job_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def bg_show(job_id: str, nation_name: str) -> None:
    """Print the captured output of a background job."""
    config = AnthillConfig.load()
    job = load_job(job_id, nation_dir(config.home, nation_name))
    if job is None:
        console.print(f"[red]No background job matching '{job_id}'.[/red]")
        return
    console.print(f"[bold]Job[/bold] {job.job_id}  [dim]({job.status})[/dim]")
    console.print(f"[bold]Request[/bold] {job.request}")
    console.print()
    text = read_log(job)
    if text:
        console.print(text)
    else:
        console.print("[dim]No output captured yet.[/dim]")


@bg.command("cancel")
@click.argument("job_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def bg_cancel(job_id: str, nation_name: str) -> None:
    """Signal a running background job to stop."""
    config = AnthillConfig.load()
    if cancel_job(job_id, nation_dir(config.home, nation_name)):
        console.print(f"[yellow]Cancellation signal sent[/yellow] to {job_id}.")
    else:
        console.print(
            f"[red]Could not cancel '{job_id}'[/red] "
            f"[dim](not found, or already finished)[/dim]"
        )


@bg.command("clear")
@click.argument("job_id")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
def bg_clear(job_id: str, nation_name: str) -> None:
    """Delete a finished job's directory. Won't touch a running one."""
    config = AnthillConfig.load()
    if clear_job(job_id, nation_dir(config.home, nation_name)):
        console.print(f"[green]Removed[/green] {job_id}.")
    else:
        console.print(
            f"[red]Could not remove '{job_id}'[/red] "
            f"[dim](still running, or not found)[/dim]"
        )


@cli.command()
@click.option("--host", default=None, help="Bind host (default 0.0.0.0).")
@click.option("--port", default=None, type=int, help="Bind port (default 8765).")
@click.option("--nation", "nation_name", default=None, help="Which nation to serve.")
def serve(host: str | None, port: int | None, nation_name: str | None) -> None:
    """Run the IM webhook daemon (FastAPI + uvicorn).

    Configure inbound channels with the CLI first:
      anthill channel add larkbot --kind lark --app-id ... --app-secret ...
      anthill channel add tgbot   --kind telegram --bot-token ...
      anthill channel add slackbot --kind slack   --bot-token ...

    Then point each bot's webhook at the matching endpoint:
      http://<your-host>:<port>/lark/webhook
      http://<your-host>:<port>/telegram/webhook
      http://<your-host>:<port>/slack/events

    (Env vars like ANTHILL_LARK_APP_ID are still honored as a fallback
    so containers and CI can wire channels without touching the CLI,
    but the CLI is the recommended path for normal use.)
    """
    try:
        from anthill.channels.daemon import DaemonConfig, serve as _serve
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        return
    cfg = DaemonConfig.from_env()
    if host is not None:
        cfg.host = host
    if port is not None:
        cfg.port = port
    if nation_name is not None:
        cfg.nation_name = nation_name
    _serve(cfg)


@cli.group()
def plugins() -> None:
    """Inspect and test built-in plugins (web_fetch, web_search, ...)."""


@plugins.command("list")
def plugins_list() -> None:
    """List all registered plugins."""
    console.print("[bold]Registered plugins[/bold]")
    console.print(default_registry.describe())


@plugins.command("call")
@click.argument("name")
@click.option("--arg", "args", multiple=True, help="key=value (repeatable)")
def plugins_call(name: str, args: tuple[str, ...]) -> None:
    """Call a plugin by name with key=value args.

    Example:
      anthill plugins call web_search --arg "query=anthill agent github"
      anthill plugins call web_fetch  --arg "url=https://example.com"
    """
    plugin = default_registry.get(name)
    if plugin is None:
        console.print(f"[red]No plugin named '{name}'.[/red]")
        return
    kwargs: dict = {}
    for raw in args:
        if "=" not in raw:
            console.print(f"[yellow]Skipping malformed arg: {raw!r}[/yellow]")
            continue
        k, v = raw.split("=", 1)
        kwargs[k.strip()] = v
    import asyncio as _asyncio
    result = _asyncio.run(plugin.call(**kwargs))
    if result.ok:
        console.print(f"[green]ok[/green]  metadata={result.metadata}")
        console.print(str(result.output)[:2000])
    else:
        console.print(f"[red]error: {result.error}[/red]")


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


@history.command("failures")
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option(
    "--since-days",
    default=0,
    help="Only count failures from the last N days (0 = all).",
)
def history_failures(nation_name: str, since_days: int) -> None:
    """Aggregate failure reasons across the nation's history (v0.5)."""
    from collections import Counter
    config = AnthillConfig.load()
    entries = load_history(nation_dir(config.home, nation_name))
    if not entries:
        console.print("[dim]No history yet.[/dim]")
        return
    since = (time.time() - since_days * 86_400) if since_days > 0 else 0.0
    by_reason: Counter[str] = Counter()
    by_citizen: dict[str, Counter[str]] = {}
    total_attempts = 0
    total_failed_attempts = 0
    for entry in entries:
        if entry.timestamp < since:
            continue
        for outcome in entry.outcomes:
            reasons = outcome.get("failure_reasons") or []
            agent_id = outcome.get("agent_id") or "?"
            for r in reasons:
                total_attempts += 1
                if r:
                    total_failed_attempts += 1
                    by_reason[r] += 1
                    by_citizen.setdefault(agent_id, Counter())[r] += 1
    if total_attempts == 0:
        console.print("[dim]No attempts recorded yet.[/dim]")
        return
    rate = total_failed_attempts / total_attempts
    console.print(
        f"[bold]Failure summary[/bold] — {nation_name}  "
        f"[dim]({total_failed_attempts}/{total_attempts} attempts, "
        f"{rate:.0%} fail rate)[/dim]"
    )
    if not by_reason:
        console.print("[green]No classified failures in this window.[/green]")
        return
    table = Table(title="By reason")
    table.add_column("Reason", style="cyan")
    table.add_column("Count", justify="right")
    table.add_column("Share", justify="right", style="dim")
    for reason, count in by_reason.most_common():
        table.add_row(reason, str(count), f"{count / total_failed_attempts:.0%}")
    console.print(table)

    if by_citizen:
        table2 = Table(title="By citizen (top reasons each)")
        table2.add_column("Citizen", style="cyan")
        table2.add_column("Failures", justify="right")
        table2.add_column("Top reason")
        for citizen_id, counter in sorted(
            by_citizen.items(), key=lambda kv: -sum(kv[1].values())
        )[:10]:
            top_reason, top_count = counter.most_common(1)[0]
            total = sum(counter.values())
            table2.add_row(
                citizen_id,
                str(total),
                f"{top_reason} ({top_count})",
            )
        console.print(table2)


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


def _parse_dim_arg(raw: str) -> tuple[str, float] | None:
    """Parse a `--dim correctness=up` / `--dim conciseness=0.2` flag.

    Accepts these value tokens:
      up / +     → 1.0
      down / -   → 0.0
      <float>    → clamped to [0, 1]
    Returns (normalized_name, score) or None when malformed.
    """
    if "=" not in raw:
        return None
    name, _, value = raw.partition("=")
    name = name.strip()
    value = value.strip().lower()
    if not name:
        return None
    if value in ("up", "+", "yes", "y"):
        score: float = 1.0
    elif value in ("down", "-", "no", "n"):
        score = 0.0
    else:
        try:
            score = max(0.0, min(1.0, float(value)))
        except ValueError:
            return None
    from anthill.core.values import normalize_dim
    key = normalize_dim(name)
    if not key:
        return None
    return key, score


@cli.command()
@click.argument("verdict", type=click.Choice(["up", "down"]))
@click.option("--nation", "nation_name", default="default", help="Nation name.")
@click.option("--weight", default=2.0, help="Rating impact magnitude.")
@click.option(
    "--dim",
    "dim_args",
    multiple=True,
    help="Per-dimension score, e.g. `--dim correctness=up --dim conciseness=0.2`. Repeatable.",
)
def rate(
    verdict: str,
    nation_name: str,
    weight: float,
    dim_args: tuple[str, ...],
) -> None:
    """Rate the last `anthill ask` up or down. Reshapes pheromone trails.

    Use `--dim NAME=VALUE` (repeatable) to add per-dimension feedback,
    which lands in the same DimensionCatalog the LLM judge writes to.
    VALUE can be `up` / `down` / `+` / `-` / a float in [0, 1].

    Example:
      anthill rate up --dim correctness=up --dim conciseness=down

    The overall verdict still reshapes pheromone strength; the dim
    scores reshape per-dimension trail data that `anthill values` and
    the router use.
    """
    config = AnthillConfig.load()
    nation = load_nation(nation_name, config.home)
    if nation is None:
        console.print(f"[red]No nation named '{nation_name}'.[/red]")
        return

    record = load_last_ask(nation_dir(config.home, nation_name))
    if record is None:
        console.print("[yellow]No recent ask to rate. Run [cyan]anthill ask[/cyan] first.[/yellow]")
        return

    dim_scores: dict[str, float] = {}
    for raw in dim_args:
        parsed = _parse_dim_arg(raw)
        if parsed is None:
            console.print(f"[yellow]Skipping malformed --dim: {raw!r}[/yellow]")
            continue
        dim_scores[parsed[0]] = parsed[1]

    touched = apply_rating(
        verdict,
        record,
        nation.pheromones,
        weight=weight,
        dim_scores=dim_scores or None,
        catalog=nation.dimension_catalog if dim_scores else None,
    )
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
    if dim_scores:
        dim_line = ", ".join(f"{k}={v:.2f}" for k, v in dim_scores.items())
        console.print(f"  [dim]dimensions: {dim_line}[/dim]")


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
