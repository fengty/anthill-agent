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

from rich.align import Align
from rich.box import ROUNDED
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from anthill import __version__
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


def _splash_banner(nation: Nation, stats: SessionStats) -> None:
    """First-impression panel — printed once at startup.

    Goals:
      - Make it visually obvious this isn't a vanilla shell
      - Communicate the "nation" metaphor in 3 seconds
      - Show what's configured so the user knows what to do next

    Kept ASCII-only so it works across terminals without font weirdness.
    Two columns: ant-colony art on the left, nation stats panel on right.
    """
    model = _current_model_name()
    model_display = (
        f"[magenta]{model}[/magenta]" if model != "(none)"
        else "[red](none — anthill model add)[/red]"
    )
    citizens_alive = sum(1 for a in nation.agents if not a.is_retired)
    citizens_total = len(nation.agents)
    citizen_line = (
        f"[bold green]{citizens_alive}[/bold green] active"
        if citizens_alive == citizens_total
        else f"[bold green]{citizens_alive}[/bold green]/[dim]{citizens_total}[/dim] active"
    )
    trail_count = len(list(nation.pheromones.trails()))
    dims = nation.dimension_catalog.known() if nation.dimension_catalog else []
    dims_line = (
        ", ".join(dims[:4]) + (f" +{len(dims) - 4}" if len(dims) > 4 else "")
        if dims else "[dim](no value dims yet — they emerge from asks)[/dim]"
    )
    vocab = list(nation.culture.task_catalog.keys()) if nation.culture else []
    vocab_line = (
        ", ".join(sorted(vocab)[:4]) + (f" +{len(vocab) - 4}" if len(vocab) > 4 else "")
        if vocab else "[dim](no task types yet)[/dim]"
    )

    # Big ASCII wordmark — pixel-aligned, monospace-safe across terminals.
    # Built from box-drawing chars so no font weirdness.
    wordmark = Text.from_markup(
        "[bold yellow]"
        "  █████╗ ███╗   ██╗████████╗██╗  ██╗██╗██╗     ██╗\n"
        " ██╔══██╗████╗  ██║╚══██╔══╝██║  ██║██║██║     ██║\n"
        " ███████║██╔██╗ ██║   ██║   ███████║██║██║     ██║\n"
        " ██╔══██║██║╚██╗██║   ██║   ██╔══██║██║██║     ██║\n"
        " ██║  ██║██║ ╚████║   ██║   ██║  ██║██║███████╗███████╗\n"
        " ╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝   ╚═╝  ╚═╝╚═╝╚══════╝╚══════╝"
        "[/bold yellow]"
    )
    tagline = Text.from_markup(
        "[dim italic] one mechanism · many models · a nation that learns who's best at what[/dim italic]"
    )

    stats_table = Table(show_header=False, box=None, padding=(0, 1), pad_edge=False)
    stats_table.add_column(style="dim", justify="right", no_wrap=True)
    stats_table.add_column(no_wrap=False)

    stats_table.add_row("nation",     f"[bold cyan]{nation.name}[/bold cyan]")
    stats_table.add_row("model",      model_display)
    stats_table.add_row("citizens",   citizen_line)
    stats_table.add_row("trails",     f"[bold]{trail_count}[/bold]")
    stats_table.add_row("vocabulary", vocab_line)
    stats_table.add_row("dimensions", dims_line)
    if stats.asks > 0:
        stats_table.add_row("session",
            f"{stats.asks} ask(s) · "
            f"{stats.tokens_in + stats.tokens_out:,} tokens · "
            f"${stats.cost_usd:.4f}"
        )

    inner = Group(
        Align.center(wordmark),
        Align.center(tagline),
        Text(""),
        stats_table,
    )
    panel = Panel(
        inner,
        title=f"[bold]anthill[/bold]  [dim]v{__version__}[/dim]",
        subtitle="[dim]type a request • /help • /quit[/dim]",
        border_style="cyan",
        box=ROUNDED,
        padding=(1, 2),
    )
    console.print(panel)


def _print_status_bar(nation: Nation, stats: SessionStats) -> None:
    """Compact one-line status between turns (post-splash).

    Different format from the splash — splash is "introduce yourself",
    status is "running tally." Includes a quality summary when v0.8
    has any dimension scores observed.
    """
    model = _current_model_name()
    cost = f"${stats.cost_usd:.4f}" if stats.cost_usd else "$0"
    tokens = f"{stats.tokens_in + stats.tokens_out:,}"
    citizens_alive = sum(1 for a in nation.agents if not a.is_retired)
    line = (
        f"[bold cyan]{nation.name}[/bold cyan] "
        f"[dim]·[/dim] [magenta]{model}[/magenta] "
        f"[dim]·[/dim] [green]{citizens_alive}[/green] citizens "
        f"[dim]·[/dim] {tokens} tokens "
        f"[dim]·[/dim] {cost}"
    )
    # Add a quality indicator if the catalog has data
    dims = nation.dimension_catalog.known() if nation.dimension_catalog else []
    if dims:
        # Average avg_score across known dimensions, excluding "cost"
        scores = [
            nation.dimension_catalog.dimensions[d].avg_score
            for d in dims if d != "cost"
        ]
        if scores:
            q = sum(scores) / len(scores)
            color = "green" if q >= 0.8 else ("yellow" if q >= 0.6 else "red")
            line += f" [dim]·[/dim] quality [{color}]{q * 100:.0f}%[/{color}]"
    console.print(line)


async def _handle_ask(
    request: str,
    nation: Nation,
    config: AnthillConfig,
    stats: SessionStats,
    *,
    deliberate: bool | None = None,  # None = auto-decide by complexity
    max_rounds: int = 3,
    quality_threshold: float = 0.85,
) -> None:
    import time

    from anthill.core.costs import UsageRecord, append_usage, price_for
    from anthill.core.executor import ProgressEvent

    # Live progress: one line per subtask, updated as state changes.
    # Keeps the REPL output scannable while still showing the king that
    # the nation is doing work (vs. just frozen).
    async def on_progress(event: ProgressEvent) -> None:
        st = event.subtask
        idx = event.index + 1
        if event.kind == "started":
            console.print(
                f"  [dim]·[/dim] [{idx}] [magenta]{st.task_type}[/magenta] "
                f"[dim]running...[/dim]"
            )
        elif event.kind == "attempt" and not event.success:
            console.print(
                f"    [yellow]retry[/yellow] attempt {event.attempt_number} failed, "
                f"trying another citizen..."
            )
        elif event.kind == "finished":
            outcome = event.outcome
            duration = outcome.duration_seconds
            if outcome.status == "ok":
                console.print(
                    f"  [green]✓[/green] [{idx}] [magenta]{st.task_type}[/magenta] "
                    f"[dim]done in {duration:.1f}s[/dim]"
                )
            elif outcome.status == "skipped":
                console.print(
                    f"  [yellow]·[/yellow] [{idx}] [magenta]{st.task_type}[/magenta] "
                    f"[dim]skipped: {outcome.skip_reason}[/dim]"
                )
            else:
                console.print(
                    f"  [red]✗[/red] [{idx}] [magenta]{st.task_type}[/magenta] "
                    f"[dim]failed after {len(outcome.attempts)} attempt(s)[/dim]"
                )

    # v0.8.1 — auto policy: if the caller didn't force on/off, use the
    # complexity heuristic. Trivial requests should never trigger
    # deliberation; complex ones should default to it. For "normal" we
    # also enable deliberation (judge / quality threshold will short-
    # circuit on round 1 if the first answer is already fine).
    if deliberate is None:
        from anthill.core.complexity import fast_classify
        fast = fast_classify(request)
        deliberate = fast != "trivial"

    if deliberate:
        from anthill.core.deliberate import (
            DeliberationRound,
            deliberate as run_deliberate,
        )

        async def _on_round(r: DeliberationRound) -> None:
            qpct = r.quality * 100
            dims = ", ".join(
                f"{k}={v:.2f}" for k, v in sorted(r.quality_by_dim.items())
            ) if r.quality_by_dim else "no dims yet"
            console.print(
                f"  [bold cyan]round {r.round_num}[/bold cyan]  "
                f"quality [bold]{qpct:.0f}%[/bold]  "
                f"[dim]({dims})[/dim]"
            )
            if r.critique:
                snippet = r.critique[:200].replace("\n", " ")
                console.print(f"  [dim]critique: {snippet}…[/dim]")

        delib = await run_deliberate(
            nation, request,
            max_rounds=max_rounds,
            quality_threshold=quality_threshold,
            on_progress=on_progress,
            nation_dir=nation_dir(config.home, nation.name),
            on_round=_on_round,
        )
        result = delib.final_round.ask_result
        # Surface the trajectory after the loop
        traj = " → ".join(f"{q*100:.0f}%" for q in delib.quality_trajectory)
        verdict_color = "green" if delib.converged else "yellow"
        console.print(
            f"  [bold {verdict_color}]{delib.stop_reason}[/bold {verdict_color}]  "
            f"[dim](rounds: {traj})[/dim]"
        )
    else:
        result = await nation.ask(
            request,
            on_progress=on_progress,
            nation_dir=nation_dir(config.home, nation.name),
        )
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

    # Plan + complexity badge (v0.8.1).
    complexity = getattr(result.plan, "complexity", "normal")
    if len(result.plan) > 1 or complexity != "normal":
        console.print()
        complexity_color = {
            "trivial": "dim",
            "normal":  "cyan",
            "complex": "yellow",
        }.get(complexity, "cyan")
        bits = [f"[dim]complexity:[/dim] [{complexity_color}]{complexity}[/{complexity_color}]"]
        if len(result.plan) > 1:
            bits.append("[dim]plan:[/dim] " + " → ".join(
                f"[magenta]{s.task_type}[/magenta]" for s in result.plan.subtasks
            ))
        console.print("  ".join(bits))
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
    _splash_banner(nation, stats)
    console.print()
    # Hint if model is unconfigured — the only real blocker for the first ask.
    if _current_model_name() == "(none)":
        console.print(
            "[yellow]No model configured yet.[/yellow]  "
            "Run [cyan]anthill setup[/cyan] for an interactive walkthrough,"
        )
        console.print(
            "  or [cyan]anthill model add deepseek "
            "--provider deepseek --model deepseek-chat --key sk-...[/cyan]"
        )
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
