"""Interactive REPL — what `anthill` does with no subcommand.

The Hermes way: typing `anthill` drops you into a conversation loop.
Slash commands let you inspect state and switch models without leaving.

Design notes:
- Status bar above the prompt: nation, model, session-token total,
  session-cost total. Refreshed before every prompt.
- Ctrl+C during a request cancels JUST that request, not the REPL.
- Ctrl+D / Ctrl+C at the empty prompt is the only way to exit
  (besides /quit). This matches Claude CLI / Hermes behaviour.
- Lines starting with `/` are commands; anything else is an ask.
- Input language is irrelevant — routing is the same.

We deliberately avoid a fancy TUI: this is a basic readline loop.
"""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.table import Table

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
from anthill.core.history import append_history, build_entry_from_ask, load_history
from anthill.core.nation import Nation
from anthill.core.persistence import load_nation, nation_dir, save_nation
from anthill.core.power import compute_ages, compute_power
from anthill.core.router import RouterConfig
from anthill.core.userconfig import load_config


console = Console()


HELP_TEXT = """[bold]REPL commands[/bold]

  Just type a question to send it to the nation.

  [bold]Inspect[/bold]
    /trails       pheromone map
    /identity     who this nation has become
    /power        national strength + ages
    /status       compact status card (model, citizens, cost so far)
    /history      recent asks

  [bold]Steer[/bold]
    /rate up      strengthen pheromones for the last answer
    /rate down    erode pheromones for the last answer
    /model        list configured models
    /model use X  switch default model
    /nation X     switch to a different nation (creates if missing)

  [bold]Session[/bold]
    /clear        clear screen (nation state preserved)
    /help, /?     this help
    /quit, /q     exit
"""


class SessionStats:
    """Per-session running totals shown in the status bar."""

    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.asks = 0

    def add(self, input_tokens: int, output_tokens: int, cost: float) -> None:
        self.tokens_in += input_tokens
        self.tokens_out += output_tokens
        self.cost_usd += cost

    def increment_ask(self) -> None:
        self.asks += 1


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
            # Resolve a model name to spawn citizens with. Prefer the user
            # config's default; fall back to AnthillConfig.default_model.
            user_cfg = load_config()
            citizen_model = user_cfg.default_model or config.default_model
            nation.spawn(count=3, model=citizen_model)
        save_nation(nation, config.home)
        console.print(
            f"[dim]Founded nation '{nation.name}' with "
            f"{len(nation.agents)} citizens.[/dim]"
        )
    return nation


def _current_model_name() -> str:
    cfg = load_config()
    return cfg.default_model or "(none — anthill model add)"


def _print_status_bar(nation: Nation, stats: SessionStats) -> None:
    model = _current_model_name()
    cost = f"${stats.cost_usd:.4f}" if stats.cost_usd else "$0"
    tokens = f"{stats.tokens_in + stats.tokens_out:,}"
    line = (
        f"[bold cyan]{nation.name}[/bold cyan] "
        f"[dim]·[/dim] model [magenta]{model}[/magenta] "
        f"[dim]·[/dim] citizens [bold]{len(nation.agents)}[/bold] "
        f"[dim]·[/dim] tokens {tokens} "
        f"[dim]·[/dim] cost {cost}"
    )
    console.print(line)


async def _handle_ask(
    request: str,
    nation: Nation,
    config: AnthillConfig,
    stats: SessionStats,
) -> None:
    import time

    from anthill.core.costs import UsageRecord, append_usage, price_for

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
            in_per_m, out_per_m = price_for(model)
            stats.add(
                attempt.input_tokens,
                attempt.output_tokens,
                (attempt.input_tokens * in_per_m + attempt.output_tokens * out_per_m) / 1_000_000,
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


def _show_status(nation: Nation, stats: SessionStats) -> None:
    console.print(f"[bold]Nation[/bold]    {nation.name}")
    console.print(f"[bold]Citizens[/bold]  {len(nation.agents)}")
    console.print(f"[bold]Model[/bold]     {_current_model_name()}")
    console.print(f"[bold]Asks[/bold]      {stats.asks} this session")
    console.print(
        f"[bold]Tokens[/bold]    in={stats.tokens_in:,} out={stats.tokens_out:,}"
    )
    console.print(f"[bold]Cost[/bold]      ${stats.cost_usd:.4f} this session")


def _show_history(config: AnthillConfig, nation: Nation, limit: int = 10) -> None:
    entries = load_history(nation_dir(config.home, nation.name), limit=limit)
    if not entries:
        console.print("[dim]No history yet.[/dim]")
        return
    for e in entries:
        statuses = [o["status"] for o in e.outcomes]
        marker = "[green]ok[/green]" if all(s == "ok" for s in statuses) else "[yellow]mixed[/yellow]"
        request = e.request if len(e.request) <= 70 else e.request[:67] + "..."
        console.print(f"  [cyan]{e.id}[/cyan]  {marker}  {request}")


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


def _handle_model_cmd(rest: str) -> None:
    """In-REPL /model subcommand."""
    from anthill.core.userconfig import load_config, save_config

    rest = rest.strip()
    cfg = load_config()
    if not rest or rest == "list":
        if not cfg.models:
            console.print("[dim]No models configured.[/dim] Run [cyan]anthill model add[/cyan].")
            return
        for m in cfg.models:
            star = "★ " if m.name == cfg.default_model else "  "
            console.print(f"  {star}[cyan]{m.name}[/cyan]  [dim]{m.provider} / {m.model}[/dim]")
        return
    if rest.startswith("use "):
        target = rest[4:].strip()
        if cfg.find_model(target) is None:
            console.print(f"[red]No model named '{target}'.[/red]")
            return
        cfg.default_model = target
        save_config(cfg)
        console.print(f"[green]Default model is now '{target}'.[/green]")
        return
    console.print("[yellow]Usage: /model | /model use NAME[/yellow]")


def _handle_nation_switch(
    rest: str, current: Nation, config: AnthillConfig
) -> Nation:
    """In-REPL /nation NAME switch."""
    target = rest.strip()
    if not target:
        console.print(f"  current: [cyan]{current.name}[/cyan]")
        # List others.
        nations_root = config.home / "nations"
        if nations_root.exists():
            others = sorted(p.name for p in nations_root.iterdir() if p.is_dir())
            if others:
                console.print("  available: " + ", ".join(others))
        return current
    refreshed = load_nation(target, config.home)
    if refreshed is None:
        # Auto-create on the fly.
        refreshed = Nation(
            name=target,
            router_config=RouterConfig(exploration=config.exploration_rate),
        )
        save_nation(refreshed, config.home)
        console.print(f"[green]Founded nation '{target}'.[/green]")
    else:
        console.print(f"[green]Switched to nation '{target}'.[/green]")
    return refreshed


def run_repl(nation_name: str = "default") -> int:
    """Drop into the REPL loop. Returns process exit code."""
    config = AnthillConfig.load()
    config.ensure_home()
    nation = _ensure_nation(config, nation_name)
    stats = SessionStats()

    console.print()
    _print_status_bar(nation, stats)
    console.print("[dim]Type a request or /help. Ctrl+D or /quit to exit.[/dim]")
    console.print()

    while True:
        try:
            line = input("» ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print()
            console.print("[dim]bye.[/dim]")
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
            elif cmd == "status":
                _show_status(nation, stats)
            elif cmd == "history":
                _show_history(config, nation)
            elif cmd == "clear":
                console.clear()
                _print_status_bar(nation, stats)
            elif cmd == "model":
                _handle_model_cmd(rest)
            elif cmd == "nation":
                nation = _handle_nation_switch(rest, nation, config)
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

        # Ask path. Ctrl+C during an ask cancels just the ask.
        stats.increment_ask()
        try:
            asyncio.run(_handle_ask(line, nation, config, stats))
        except KeyboardInterrupt:
            console.print("[yellow](cancelled)[/yellow]")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Error: {e}[/red]")

        # Reload nation after each ask so persisted state stays in sync.
        refreshed = load_nation(nation.name, config.home)
        if refreshed is not None:
            nation = refreshed

        # Status bar refresh between turns.
        _print_status_bar(nation, stats)

    return 0
