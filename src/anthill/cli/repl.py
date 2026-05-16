"""Interactive REPL — what `anthill` does when invoked with no subcommand.

The Hermes way: typing `anthill` drops you straight into a conversation
loop. You ask, the nation answers. Subcommands stay available; the REPL
is the *default* experience for a king who just wants to talk to their
nation.

Design notes:

- We auto-create a 'default' nation if none exists and spawn a few
  citizens so the very first request works without a setup ceremony.
- We auto-save after every ask, so persistence works without explicit
  saves.
- Lines starting with `/` are REPL commands (a small set: /help,
  /trails, /identity, /power, /quit). Anything else is an ask.
- The REPL talks Chinese OR English — both routes the same.

We deliberately avoid a fancy TUI: this is a basic readline loop. A
TUI is v0.2+ territory; today's priority is "you type, it works."
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.table import Table

from anthill.config import AnthillConfig
from anthill.core.feedback import AskRecord, append_exemplar, apply_rating, load_last_ask, save_last_ask
from anthill.core.feedback import Exemplar
from anthill.core.history import append_history, build_entry_from_ask
from anthill.core.nation import Nation
from anthill.core.persistence import load_nation, nation_dir, save_nation
from anthill.core.power import compute_ages, compute_power
from anthill.core.router import RouterConfig
from anthill.core.history import load_history
from anthill.core.feedback import load_exemplars


console = Console()


HELP_TEXT = """[bold]REPL commands[/bold]

  Just type a question to send it to the nation:
    > 用一句话解释什么是熵
    > Translate hello to Chinese

  Slash commands:
    /help        show this help
    /trails      show pheromone map
    /identity    show what the nation has become
    /power       show national strength
    /rate up     rate the last answer positively
    /rate down   rate the last answer negatively
    /quit        leave the REPL
"""


def _ensure_nation(config: AnthillConfig, name: str = "default") -> Nation:
    """Get the nation, founding it with a default cohort if missing."""
    nation = load_nation(name, config.home)
    if nation is None or not nation.agents:
        if nation is None:
            nation = Nation(
                name=name,
                router_config=RouterConfig(exploration=config.exploration_rate),
            )
        if not nation.agents:
            nation.spawn(count=3, model=config.default_model)
        save_nation(nation, config.home)
        console.print(
            f"[dim]Founded a default nation with 3 citizens "
            f"using {config.default_model}.[/dim]"
        )
    return nation


async def _handle_ask(request: str, nation: Nation, config: AnthillConfig) -> None:
    import time
    from anthill.core.costs import UsageRecord, append_usage

    result = await nation.ask(request)
    save_nation(nation, config.home)

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
            nation_dir(config.home, nation.name),
        )
    append_history(
        build_entry_from_ask(request, result.plan.subtasks, result.outcomes),
        nation_dir(config.home, nation.name),
    )
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
                nation_dir(config.home, nation.name),
            )

    if len(result.plan) > 1:
        console.print()
        console.print("[dim]plan:[/dim] " + " -> ".join(
            f"[magenta]{s.task_type}[/magenta]" for s in result.plan.subtasks
        ))
    console.print()
    console.print(result.final_output)
    console.print()


def _show_trails(nation: Nation) -> None:
    table = Table(title="Pheromone trails")
    table.add_column("Citizen", style="cyan")
    table.add_column("Task type", style="magenta")
    table.add_column("Strength", style="green", justify="right")
    trails = sorted(nation.pheromones.trails(), key=lambda t: t.strength, reverse=True)
    for t in trails:
        table.add_row(t.agent_id, t.task_type, f"{t.strength:.2f}")
    console.print(table)


def _show_identity(nation: Nation) -> None:
    console.print(f"[bold]Nation[/bold]    {nation.name}")
    console.print(f"[bold]Citizens[/bold]  {len(nation.agents)}")
    console.print(nation.culture.summarize())


def _show_power(nation: Nation, config: AnthillConfig) -> None:
    history = load_history(nation_dir(config.home, nation.name))
    exemplars = load_exemplars(nation_dir(config.home, nation.name))
    report = compute_power(nation, history, exemplars)
    ages = compute_ages(nation, history, exemplars)
    console.print(f"national strength: [bold]{report.overall:.1f}[/bold] / 100")
    for age in ages:
        icon = "✓" if age.completed else "·"
        console.print(f"  {icon} {age.name}  [dim]{age.description}[/dim]")


def _handle_rate(verdict: str, nation: Nation, config: AnthillConfig) -> None:
    import time
    record = load_last_ask(nation_dir(config.home, nation.name))
    if record is None:
        console.print("[yellow]No recent ask to rate.[/yellow]")
        return
    apply_rating(verdict, record, nation.pheromones)
    save_nation(nation, config.home)
    if record.final_output:
        append_exemplar(
            Exemplar(
                rating=verdict,
                request=record.request,
                output=record.final_output,
                timestamp=time.time(),
            ),
            nation_dir(config.home, nation.name),
        )
    console.print(f"[green]Rating '{verdict}' applied.[/green]")


def run_repl(nation_name: str = "default") -> int:
    """Drop into the REPL loop. Returns process exit code."""
    config = AnthillConfig.load()
    config.ensure_home()
    nation = _ensure_nation(config, nation_name)

    console.print(f"[bold cyan]Anthill[/bold cyan] — {nation.name} "
                  f"([dim]{len(nation.agents)} citizens[/dim])")
    console.print("[dim]Type your request, or /help for commands. /quit to exit.[/dim]")
    console.print()

    while True:
        try:
            line = input("» ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print()
            console.print("[dim]Bye.[/dim]")
            return 0

        if not line:
            continue

        if line.startswith("/"):
            cmd, _, rest = line[1:].partition(" ")
            cmd = cmd.lower()
            if cmd in ("quit", "exit", "q"):
                return 0
            if cmd in ("help", "h", "?"):
                console.print(HELP_TEXT)
            elif cmd == "trails":
                _show_trails(nation)
            elif cmd == "identity":
                _show_identity(nation)
            elif cmd == "power":
                _show_power(nation, config)
            elif cmd == "rate":
                verdict = rest.strip().lower()
                if verdict in ("up", "down"):
                    _handle_rate(verdict, nation, config)
                    nation = load_nation(nation.name, config.home) or nation
                else:
                    console.print("[yellow]Usage: /rate up | /rate down[/yellow]")
            else:
                console.print(f"[yellow]Unknown command: /{cmd}.[/yellow] Try /help.")
            continue

        try:
            asyncio.run(_handle_ask(line, nation, config))
        except KeyboardInterrupt:
            console.print("[yellow](cancelled)[/yellow]")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Error: {e}[/red]")
        # Reload nation after each ask so persistence (pheromones, cache) is
        # reflected even if the user's next command reads from disk paths.
        refreshed = load_nation(nation.name, config.home)
        if refreshed is not None:
            nation = refreshed

    return 0
