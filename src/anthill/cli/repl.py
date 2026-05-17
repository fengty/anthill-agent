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
import atexit
from pathlib import Path

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


# 0.1.5+ — readline integration. Wiring it up just by importing the
# module is enough on POSIX: input() then auto-supports left/right
# arrow editing, up/down history, Ctrl+A/E/K/U/W/R search. Persist
# history so it survives session restarts.
def _setup_readline(home: Path) -> None:
    """Enable arrow-key history + line editing in input(). POSIX only.

    Silent no-op on platforms without readline (vanilla Windows). The
    history file lives in ~/.anthill/repl_history; capped at 1000
    lines so it doesn't grow unbounded over years of use.
    """
    try:
        import readline  # noqa: PLC0415 — optional module
    except ImportError:
        return

    history_file = home / "repl_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(str(history_file))
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(1000)

    def _save() -> None:
        try:
            readline.write_history_file(str(history_file))
        except OSError:
            pass

    atexit.register(_save)


# v0.1.12 — multi-line input. Typing `"""` alone (or `"""text...`)
# enters heredoc mode; lines accumulate until a closing `"""` is seen.
# This lets users paste code snippets or long prompts without the
# newline auto-submitting after the first line.
MULTILINE_OPENER = '"""'
MULTILINE_CONT_PROMPT = "  ... "


def _read_request_line(prompt: str = "» ") -> str:
    '''Read one logical request from stdin.

    Normal case: returns a single line of stripped input. When the
    line starts with the triple-quote opener, switches into multi-
    line mode and accumulates lines until a closing token is seen on
    its own (or at the end of a line). The returned string has the
    surrounding triple-quotes stripped.

    Ctrl+C inside multi-line mode discards the buffer and bubbles up
    to the caller (top-level REPL treats it as "cancel this request,
    go back to the prompt"). EOF (Ctrl+D) inside multi-line submits
    what has been accumulated so far — handy for piped input.
    '''
    first = input(prompt)
    stripped = first.strip()
    if not stripped.startswith(MULTILINE_OPENER):
        return stripped

    # Multi-line mode. Strip the leading `"""` from the first line; if
    # what remains already contains a closing `"""`, the user wrote
    # `"""hello"""` inline and we're done in one line.
    remainder = stripped[len(MULTILINE_OPENER) :]
    if remainder.endswith(MULTILINE_OPENER) and len(remainder) >= len(MULTILINE_OPENER):
        return remainder[: -len(MULTILINE_OPENER)].strip()

    buffer: list[str] = []
    if remainder:
        buffer.append(remainder)

    console.print('[dim]  (multi-line mode — end with """ on its own line)[/dim]')
    while True:
        try:
            line = input(MULTILINE_CONT_PROMPT)
        except EOFError:
            # Treat Ctrl+D as "submit what we have so far".
            break
        # Closing token: either standalone `"""` or trailing `"""`.
        stripped_line = line.rstrip()
        if stripped_line == MULTILINE_OPENER:
            break
        if stripped_line.endswith(MULTILINE_OPENER):
            buffer.append(stripped_line[: -len(MULTILINE_OPENER)])
            break
        # Preserve the line verbatim — including leading whitespace,
        # which matters for code paste.
        buffer.append(line)
    # rstrip-only: trailing blank lines / whitespace get trimmed but
    # leading indentation of the first content line survives — code
    # paste depends on it.
    return "\n".join(buffer).rstrip()


_AUTH_HINT_THRESHOLD = 3


def _track_auth_failure(event, nation: Nation, stats: "SessionStats") -> None:  # noqa: ANN001
    """Count consecutive AUTH failures by model name; nudge on threshold.

    When the same configured model keeps getting auth-rejected we know
    the key in secrets.toml is wrong — distinct from the "model name
    not in config" case the startup preflight already covers. Surface
    a remedy hint once per session per model.
    """
    attempts = event.outcome.attempts
    latest = attempts[-1] if attempts else None
    if latest is None:
        return
    if latest.failure_reason != "auth":
        return
    # Resolve which model NAME (ModelEntry name) the failing citizen runs on.
    model_name = next(
        (a.model for a in nation.agents if a.id == latest.agent_id),
        None,
    )
    if model_name is None:
        return
    stats.auth_failures_by_model[model_name] = (
        stats.auth_failures_by_model.get(model_name, 0) + 1
    )
    if stats.auth_failures_by_model[model_name] < _AUTH_HINT_THRESHOLD:
        return
    if model_name in stats.auth_fix_hinted:
        return
    stats.auth_fix_hinted.add(model_name)
    from anthill.core.userconfig import load_config
    cfg = load_config()
    default = cfg.default_model
    console.print(
        f"  [yellow]💡 model [cyan]{model_name}[/cyan] has failed auth "
        f"{stats.auth_failures_by_model[model_name]}× this session.[/yellow]"
    )
    console.print(
        "  [dim]The key in secrets.toml is probably wrong. Try:[/dim]"
    )
    console.print(
        f"  [dim]·[/dim] [cyan]/model test {model_name}[/cyan] "
        f"[dim](verify the key)[/dim]"
    )
    if default and default != model_name:
        console.print(
            f"  [dim]·[/dim] [cyan]/citizens migrate {model_name}[/cyan] "
            f"[dim](move all citizens off it onto '{default}')[/dim]"
        )
    console.print(
        f"  [dim]·[/dim] [cyan]/model rm {model_name}[/cyan] "
        f"[dim](delete + re-add with a fresh key)[/dim]"
    )


def _citizen_model_preflight(nation: Nation) -> None:
    """Warn at startup when citizens point at unresolvable model names.

    The scenario: user ran setup with provider A, the nation got 3
    citizens with model="A". Then they reconfigured under a different
    name; old citizens still say "A" but UserConfig no longer has
    a ModelEntry named "A". Every ask becomes 3 auth failures.

    Strategy: spot the gap and offer a one-liner remedy. The
    interactive fix is `/citizens migrate` which calls
    `migrate_citizens_to(default_model)`.
    """
    from anthill.core.citizen_check import find_unresolvable_citizens
    from anthill.core.userconfig import load_config

    cfg = load_config()
    configured = [m.name for m in cfg.models]
    issues = find_unresolvable_citizens(nation.agents, configured)
    if not issues:
        return

    # Group by model name so the message stays compact when 3 citizens
    # share the same broken model (typical case).
    grouped: dict[str, list[str]] = {}
    for issue in issues:
        grouped.setdefault(issue.model, []).append(issue.agent_id)

    # 0.1.22 — surface "stale env var" cases distinctly. Users see "I
    # have MINIMAX_API_KEY exported but every ask still fails" exactly
    # when this triggers, and the remedy is the same migration step.
    reasons_by_model: dict[str, str] = {i.model: i.reason for i in issues}
    any_stale = any(r == "stale_legacy" for r in reasons_by_model.values())

    console.print(
        f"[yellow]⚠ {len(issues)} citizen(s) point at model name(s) "
        "you no longer have configured:[/yellow]"
    )
    for model_name, citizen_ids in grouped.items():
        sample = ", ".join(citizen_ids[:3])
        more = f" (+{len(citizen_ids) - 3} more)" if len(citizen_ids) > 3 else ""
        suffix = ""
        if reasons_by_model.get(model_name) == "stale_legacy":
            suffix = " [dim](env var set but UserConfig now owns auth)[/dim]"
        console.print(
            f"  [dim]·[/dim] [red]{model_name}[/red]{suffix} "
            f"used by {sample}{more}"
        )
    if any_stale:
        console.print(
            "  [dim]Those citizens would call legacy env-var providers "
            "and likely hit (auth) errors.[/dim]"
        )
    if cfg.default_model:
        console.print(
            f"  [dim]Fix:[/dim] [cyan]/citizens migrate[/cyan] "
            f"[dim](point all unresolvable citizens at "
            f"'{cfg.default_model}')[/dim]"
        )
    else:
        console.print(
            "  [dim]Fix:[/dim] configure a model with "
            "[cyan]/setup[/cyan] [dim]first, then run "
            "[cyan]/citizens migrate[/cyan][/dim]"
        )
    console.print()


def _proxy_preflight() -> None:
    """Warn early when a proxy env var is set but the transport can't use it.

    The exact case we hit in 0.1.19: a user has ``ALL_PROXY=socks5://...``
    in their shell (common with shadowsocks / clash). httpx auto-respects
    the env var, but won't follow socks:// without the ``socksio`` extra
    installed. Every provider call then errors with "SOCKS proxy, but
    the 'socksio' package is not installed" and the REPL just shows
    "retry failed (unknown)" three times.

    0.1.20 pins ``httpx[socks]`` in our deps so this won't happen on a
    fresh install — but anyone upgrading from <0.1.20 keeps the old
    install, so the early warning still earns its keep.
    """
    import os

    proxy_var = next(
        (v for v in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy")
         if os.environ.get(v)),
        None,
    )
    if proxy_var is None:
        return
    proxy_value = os.environ[proxy_var]
    if not proxy_value.lower().startswith(("socks4://", "socks5://", "socks5h://")):
        return  # http/https proxy is fine without an extra
    try:
        import socksio  # noqa: F401, PLC0415
    except ImportError:
        console.print(
            f"[yellow]⚠ {proxy_var}={proxy_value} is a SOCKS proxy, "
            f"but the 'socksio' package is not installed.[/yellow]"
        )
        console.print(
            "  [dim]Fix one of:[/dim]\n"
            "  [dim]·[/dim] [cyan]pip install 'httpx[socks]'[/cyan] "
            "[dim](or re-run the install one-liner)[/dim]\n"
            f"  [dim]·[/dim] [cyan]unset {proxy_var}[/cyan] "
            "[dim](if you didn't mean to route Anthill through it)[/dim]"
        )
        console.print()


HELP_TEXT = """[bold]REPL commands[/bold]

  Just type a question to send it to the nation.

  [bold]Inspect[/bold]
    /trails       pheromone map
    /identity     who this nation has become
    /power        national strength + ages
    /status       compact status card (model, citizens, cost so far)
    /history      recent asks
    /project      project context Scout sees (cwd, git branch, files)
    /skills       recurring patterns the nation has noticed in history
    /citizens          list alive citizens + which models they use
    /citizens migrate  point all unresolvable citizens at the default
    /citizens migrate X
                       evacuate every citizen running on model X
                       (use when X is configured but its key is bad)

  [bold]Steer[/bold]
    /rate up      strengthen pheromones for the last answer
    /rate down    erode pheromones for the last answer
    /model         list configured models (numbered)
    /model use X   switch default model (X = name or list index)
    /model rm X    delete a model (X = name or index)
    /model rm      interactive — walk each model with y/N
    /model test X  verify a model's API key (the (auth) diagnostic)
    /nation X     switch to a different nation (creates if missing)
    /plan         toggle plan review (skip/keep subtasks before run)
    /setup        relaunch the interactive setup wizard

  [bold]Session[/bold]
    /clear        clear screen (nation state preserved)
    /help, /?     this help
    /quit, /q     exit

  [bold]Editing[/bold]
    ←/→           move within the line
    ↑/↓           previous / next from history (saved across sessions)
    Ctrl+A / E    jump to line start / end
    Ctrl+R        reverse-search history
    Tab           complete slash commands, model / nation names, @paths
    \"\"\"           start a multi-line block; close with \"\"\" on its own line

  [bold]Attachments[/bold]
    @path/to/file       attach a file as context
    @src/**/*.py        glob — every matching file is read
    [dim]File size cap 100 KB each, 500 KB total. Binary files skipped.[/dim]
"""


class SessionStats:
    """Per-session running totals shown in the status bar."""

    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.asks = 0
        # v0.1.13 — opt-in plan review. When True, Scout's plan goes
        # through an interactive prompt before execution so the user
        # can skip / keep subtasks or cancel. Toggle with /plan.
        self.plan_review = False
        # 0.1.24 — per-model running auth-failure count. When the same
        # ModelEntry returns auth errors N times in a row, surface a
        # one-time fix-it hint to the user. Keyed by the agent's
        # `model` field (which is the ModelEntry NAME, not the
        # provider's internal model id).
        self.auth_failures_by_model: dict[str, int] = {}
        self.auth_fix_hinted: set[str] = set()
        # v0.1.17 — skill-mining nudge bookkeeping. We surface a
        # "you've done this 3x — save as recipe?" hint at most once
        # per session per cluster, keyed by the cluster's first
        # history-entry id. Without this the user would get the same
        # hint after every matching ask.
        self.suggested_skill_ids: set[str] = set()

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


def _no_model_configured() -> bool:
    """True when there's no default_model set yet — the broken-state check.

    Distinct from `_current_model_name() == "(none …)"` string matching,
    which broke once the hint text changed. This reads the config directly.
    """
    cfg = load_config()
    return not cfg.default_model


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
    # 0.1.15 — show project context when the REPL launches inside a
    # detectable project root. Surfaces the binding so the user sees
    # WHICH project Scout will be aware of in subsequent asks.
    try:
        from anthill.core.project import find_project_root
        _proj = find_project_root()
    except Exception:  # noqa: BLE001 — splash must never crash
        _proj = None
    if _proj is not None:
        branch_suffix = ""
        if _proj.git_branch:
            dirty_mark = "*" if _proj.git_dirty_count > 0 else ""
            branch_suffix = f" [dim]· {_proj.git_branch}{dirty_mark}[/dim]"
        stats_table.add_row(
            "project",
            f"[bold green]{_proj.name}[/bold green] "
            f"[dim]({_proj.kind})[/dim]{branch_suffix}",
        )
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


def _onboarding_card(nation: Nation) -> None:
    """0.1.6+ — "how to play" guidance, printed right after the splash.

    Conditional on nation state:
      - No model:     handled separately by the setup gate (this card skipped)
      - Empty nation: show concrete example prompts + key commands
                      (new user needs "what can I do with this?")
      - Mature nation: show "welcome back" + memory-aware commands
                       (returning user wants to keep building on past work)

    Goal: in the 3 seconds after splash, the user should be able to type
    SOMETHING and see Anthill do work. Generic "try /help" doesn't cut it.
    """
    # Determine nation maturity by counting SUCCESSFUL asks — entries
    # where at least one subtask outcome reached status='ok'. A history
    # full of failed asks shouldn't trigger "welcome back, you've done
    # so much": that just rubs salt in the user's wound.
    history_path = nation.history_path
    n_successful = 0
    if history_path is not None and history_path.exists():
        try:
            import json as _json
            with history_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    outcomes = entry.get("outcomes") or []
                    if any(o.get("status") == "ok" for o in outcomes):
                        n_successful += 1
        except OSError:
            n_successful = 0

    if n_successful >= 3:
        # Mature nation — welcome back card
        vocab = sorted(nation.culture.task_catalog.keys()) if nation.culture else []
        vocab_line = (
            ", ".join(vocab[:5]) + (f" +{len(vocab) - 5}" if len(vocab) > 5 else "")
            if vocab else "no task types yet"
        )
        console.print(
            f"[bold]👋 Welcome back.[/bold] This nation has completed "
            f"[bold cyan]{n_successful}[/bold cyan] ask(s) "
            f"across vocabulary: [dim]{vocab_line}[/dim]"
        )
        console.print(
            "  [cyan]/identity[/cyan]  who your nation has become      "
            "[cyan]/trails[/cyan]   who's learned what"
        )
        console.print(
            "  [cyan]/history[/cyan]  browse past asks                 "
            "[cyan]/power[/cyan]    national strength"
        )
        console.print()
        return

    # Empty / fresh nation — show concrete examples
    console.print("[bold]What this isn't:[/bold] [dim]another chat with one model.[/dim]")
    console.print(
        "[bold]What this is:[/bold] [dim]a nation of citizens that route each subtask "
        "to whoever does it best,[/dim]"
    )
    console.print(
        "                [dim]and learns who that is from real outcomes.[/dim]"
    )
    console.print()
    console.print("[bold yellow]🎯 Try one of these to see your nation in action:[/bold yellow]")
    console.print("   [magenta]»[/magenta] [dim]Explain stigmergy in one sentence[/dim]            "
                  "[dim](trivial · 1 citizen · ~3s)[/dim]")
    console.print("   [magenta]»[/magenta] [dim]Translate this and explain the choices: …[/dim]    "
                  "[dim](normal · multi-step)[/dim]")
    console.print("   [magenta]»[/magenta] [dim]Research the top 3 vector DBs and recommend[/dim]  "
                  "[dim](complex · deliberation auto-on)[/dim]")
    console.print()
    console.print("[bold]⌨  Editing[/bold]   [dim]↑↓ recall past asks · "
                  "Ctrl+R search · Ctrl+C cancels current ask only[/dim]")
    console.print("[bold]⚡ Commands[/bold]  [cyan]/help[/cyan] full list · "
                  "[cyan]/setup[/cyan] re-config wizard · "
                  "[cyan]/identity[/cyan] · [cyan]/quit[/cyan]")
    console.print()


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
    from pathlib import Path as _Path

    from anthill.core.attachments import expand_attachments
    from anthill.core.costs import UsageRecord, append_usage, price_for
    from anthill.core.executor import ProgressEvent

    # v0.1.11 — @file / @glob expansion. Read referenced files BEFORE
    # the request reaches Scout so the planner can see them as part of
    # the prompt context. The visible request stays as-typed so history
    # / plan-cache hashing remains stable across users with different
    # working directories.
    attachment_block = expand_attachments(request, base=_Path.cwd())
    if attachment_block.files:
        names = ", ".join(f.path for f in attachment_block.files[:3])
        more = (
            f" (+{len(attachment_block.files) - 3} more)"
            if len(attachment_block.files) > 3
            else ""
        )
        total_kb = sum(f.size_bytes for f in attachment_block.files) / 1024
        console.print(
            f"  [dim]📎 attached {len(attachment_block.files)} file(s): "
            f"{names}{more} · {total_kb:.1f} KB[/dim]"
        )
    for err in attachment_block.errors:
        console.print(
            f"  [yellow]⚠ skipped {err.token}[/yellow] [dim]({err.reason})[/dim]"
        )
    effective_request = attachment_block.render() + request

    # Live progress: one line per subtask, updated as state changes.
    # Keeps the REPL output scannable while still showing the king that
    # the nation is doing work (vs. just frozen).
    # Helper: agent_id → "deepseek" / "minimax" / etc. (the actual model
    # the citizen runs on). Used to surface multi-model coordination in
    # the progress stream — the user sees WHICH model handled each
    # subtask, not just a generic "running...".
    def _model_for(agent_id: str) -> str:
        for a in nation.agents:
            if a.id == agent_id:
                return a.model
        return "?"

    # v0.1.13 — plan review handler. Scout's output goes through here
    # before execution starts. The user can: hit Enter to accept, type
    # `s N[,N]` to skip subtasks, `k N[,N]` to keep only those, or `c`
    # to cancel the whole ask. Returns the (possibly modified) plan,
    # or None to signal cancel.
    async def on_plan(plan):  # noqa: ANN001, ANN202
        from anthill.core.scout import Plan as _Plan

        if not plan.subtasks:
            return plan  # nothing to review
        console.print()
        console.print(
            f"  [bold cyan]Plan[/bold cyan] "
            f"[dim]({len(plan.subtasks)} subtask(s), "
            f"complexity={plan.complexity})[/dim]"
        )
        current = list(plan.subtasks)
        while True:
            for i, st in enumerate(current, start=1):
                deps = f" [dim]← {', '.join(st.depends_on)}[/dim]" if st.depends_on else ""
                prompt_snip = st.prompt[:80].replace("\n", " ")
                if len(st.prompt) > 80:
                    prompt_snip += "…"
                console.print(
                    f"    [cyan]{i}[/cyan]. [magenta]{st.task_type}[/magenta]"
                    f"{deps}  [dim]{prompt_snip}[/dim]"
                )
            console.print(
                "  [dim]Enter=run · "
                "s N[,N]=skip · k N[,N]=keep only · c=cancel[/dim]"
            )
            try:
                choice = input("  plan? ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not choice:
                return _Plan(subtasks=current, complexity=plan.complexity)
            if choice.lower() in ("c", "cancel"):
                return None
            head, _, rest = choice.partition(" ")
            head = head.lower()
            if head in ("s", "skip", "k", "keep"):
                try:
                    indices = {
                        int(part) - 1 for part in rest.replace(",", " ").split() if part
                    }
                except ValueError:
                    console.print("  [yellow](numbers please)[/yellow]")
                    continue
                if not indices or any(
                    not 0 <= idx < len(current) for idx in indices
                ):
                    console.print(
                        f"  [yellow](valid indices: 1-{len(current)})[/yellow]"
                    )
                    continue
                if head in ("s", "skip"):
                    current = [
                        st for i, st in enumerate(current) if i not in indices
                    ]
                else:
                    current = [
                        st for i, st in enumerate(current) if i in indices
                    ]
                if not current:
                    console.print(
                        "  [yellow](no subtasks left — cancelling)[/yellow]"
                    )
                    return None
                console.print(
                    f"  [dim]updated — {len(current)} subtask(s) remaining[/dim]"
                )
                continue
            console.print("  [yellow](didn't understand — Enter, s, k, or c)[/yellow]")

    # v0.9.0 — clarification turn handler. When the clarifier inside
    # Nation.ask flags a request as ambiguous, this callback runs in
    # the REPL: print the questions, read one line of user response,
    # hand it back. Returning None means "skip clarification, proceed
    # as-is" — what `/skip` or empty input gets you.
    async def on_clarify(questions) -> str | None:  # noqa: ANN001
        console.print()
        console.print(
            f"  [yellow]?[/yellow] [dim]Need to clarify first ({questions.why}):[/dim]"
        )
        for i, q in enumerate(questions.questions, start=1):
            console.print(f"     [yellow]{i}.[/yellow] {q}")
        console.print(
            "  [dim](answer all at once, or type /skip to proceed as-is)[/dim]"
        )
        try:
            ans = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not ans or ans.lower() in ("/skip", "skip"):
            console.print("  [dim](skipped clarification, continuing with original request)[/dim]")
            return None
        return ans

    # v0.1.10: track inline-token streaming state so we know when to
    # close the dim-gutter line before printing the next discrete event.
    streaming_state = {"open": False, "chars": 0}

    def _close_stream_line() -> None:
        if streaming_state["open"]:
            console.print()  # newline to terminate the inline stream
            streaming_state["open"] = False
            streaming_state["chars"] = 0

    async def on_progress(event: ProgressEvent) -> None:
        st = event.subtask
        idx = event.index + 1
        if event.kind == "started":
            _close_stream_line()
            console.print(
                f"  [dim]·[/dim] [{idx}] [magenta]{st.task_type}[/magenta] "
                f"[dim]running...[/dim]"
            )
        elif event.kind == "token":
            # Live-stream tokens dimly under the running subtask. We
            # cap the visible width per line — long single-paragraph
            # outputs would otherwise scroll the terminal sideways.
            delta = event.delta
            if not delta:
                return
            if not streaming_state["open"]:
                # First token of this attempt — open the gutter line.
                console.print("    [dim]┊[/dim] ", end="")
                streaming_state["open"] = True
                streaming_state["chars"] = 0
            # Hard-wrap at ~80 chars so the dim gutter stays readable.
            for piece in delta.splitlines(keepends=True):
                line = piece.rstrip("\n")
                ends_with_nl = piece.endswith("\n")
                if line:
                    console.print(f"[dim]{line}[/dim]", end="")
                    streaming_state["chars"] += len(line)
                if ends_with_nl or streaming_state["chars"] >= 80:
                    console.print()
                    streaming_state["chars"] = 0
                    # If more text remains, continue with a fresh gutter.
                    streaming_state["open"] = False
                    if piece is not delta.splitlines(keepends=True)[-1]:
                        console.print("    [dim]┊[/dim] ", end="")
                        streaming_state["open"] = True
        elif event.kind == "attempt" and not event.success:
            _close_stream_line()
            # 0.1.24 — auth-failure tracking. Same ModelEntry getting
            # auth-rejected over and over is the smoking gun for a
            # bad key in secrets.toml (the "minimax model configured
            # but its key is wrong" case). Count and offer the remedy.
            _track_auth_failure(event, nation, stats)
            # 0.1.8 — surface WHAT failed, not just "failed". On the
            # latest attempt we read failure_reason (v0.5 structured)
            # and the raw output (often "[error] 404 model not found").
            # Without this, a misconfigured model id looks like "the
            # nation is broken" instead of "your model id is wrong."
            attempts = event.outcome.attempts
            latest = attempts[-1] if attempts else None
            reason = (
                getattr(latest, "failure_reason", None)
                if latest is not None
                else None
            )
            err_blurb = ""
            if latest is not None:
                # Trim long error strings; show just enough to diagnose.
                output_text = str(latest.output or "").strip()
                if output_text:
                    # Snip to one line, 100 chars.
                    snippet = output_text.replace("\n", " ")[:100]
                    err_blurb = f"  [dim]│[/dim] [red]{snippet}[/red]"
            label = f" ({reason})" if reason else ""
            console.print(
                f"    [yellow]retry[/yellow] attempt {event.attempt_number} "
                f"failed{label}, trying another citizen..."
            )
            if err_blurb:
                console.print(err_blurb)
        elif event.kind == "finished":
            _close_stream_line()
            outcome = event.outcome
            duration = outcome.duration_seconds
            if outcome.status == "ok" and outcome.final is not None:
                # v0.8.2 — show citizen + model so the user SEES multi-model
                # coordination, not just "this finished."
                agent_id = outcome.final.agent_id
                model = _model_for(agent_id)
                console.print(
                    f"  [green]✓[/green] [{idx}] [magenta]{st.task_type}[/magenta] "
                    f"[dim]→[/dim] [cyan]{agent_id[:12]}[/cyan]"
                    f"[dim]/{model}[/dim] "
                    f"[dim]({duration:.1f}s)[/dim]"
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
            nation, effective_request,
            max_rounds=max_rounds,
            quality_threshold=quality_threshold,
            on_progress=on_progress,
            on_clarify=on_clarify,  # v0.9.0
            on_plan=on_plan if stats.plan_review else None,  # v0.1.13
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
            effective_request,
            on_progress=on_progress,
            on_clarify=on_clarify,  # v0.9.0
            on_plan=on_plan if stats.plan_review else None,  # v0.1.13
            nation_dir=nation_dir(config.home, nation.name),
        )
    save_nation(nation, config.home)

    # v0.1.13 — plan review cancelled the ask. No outcomes to record;
    # don't dirty history / last-ask / usage logs.
    if getattr(result, "cancelled_by_user", False):
        console.print("  [dim]plan cancelled — nothing ran.[/dim]")
        return

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
    # 0.1.17 — skill auto-mining hint. After history is appended,
    # scan for clusters of similar past asks and nudge the user once
    # per session per cluster if the current request belongs to one
    # with ≥3 occurrences. Keeps the user in control: we *notice*
    # the pattern; they decide whether it deserves a name.
    try:
        from anthill.core.skill_mining import looks_like_new_match, mine_skills
        history_now = load_history(nation_dir(config.home, nation.name))
        for skill in mine_skills(history_now):
            cluster_key = skill.entry_ids[0]
            if cluster_key in stats.suggested_skill_ids:
                continue
            if not looks_like_new_match(skill, request):
                continue
            stats.suggested_skill_ids.add(cluster_key)
            snippet = skill.representative.replace("\n", " ")[:60]
            console.print(
                f"  [dim]💡 you've asked things like '{snippet}…' "
                f"{skill.occurrences} times. Run "
                f"[cyan]anthill recipe save[/cyan] to bake a skill.[/dim]"
            )
            break  # one hint per ask is enough
    except Exception:  # noqa: BLE001 — mining is best-effort, never break the REPL
        pass
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

    # v0.8.2 — multi-model collaboration card. Surfaces "which citizen
    # (which model) did which subtask" so the user can SEE multi-model
    # coordination, not just trust the readme. Skipped for trivial
    # single-shot asks where there's nothing interesting to show.
    if complexity != "trivial" and len(result.outcomes) > 0:
        participants = []
        models_seen: set[str] = set()
        for outcome in result.outcomes:
            if outcome.status != "ok" or outcome.final is None:
                continue
            agent_id = outcome.final.agent_id
            model = _model_for(agent_id)
            participants.append((outcome.subtask.task_type, agent_id, model))
            models_seen.add(model)
        if participants and (len(models_seen) > 1 or len(participants) > 1):
            console.print()
            # Header reflects the headline: how many distinct models actually ran.
            model_count = len(models_seen)
            if model_count > 1:
                header = (
                    f"[bold green]✓ {model_count} models collaborated[/bold green] "
                    f"[dim]·[/dim] {len(participants)} subtasks"
                )
            else:
                header = (
                    f"[dim]✓ {len(participants)} subtask(s) on a single model "
                    f"({next(iter(models_seen))})[/dim]"
                )
            console.print(header)
            for tt, aid, model in participants:
                console.print(
                    f"  [magenta]{tt}[/magenta] "
                    f"[dim]→[/dim] [cyan]{aid[:12]}[/cyan][dim]/{model}[/dim]"
                )

    # 0.1.4+ — episodic sources line. Shows when Scout actually pulled
    # from similar past asks. Empty list ⇒ no past was similar enough
    # OR the trivial/cache fast path was taken; either way the nation
    # didn't "remember" anything for this ask, so we stay quiet.
    sources = getattr(result, "episodic_sources", None) or []
    if sources:
        joined = ", ".join(f"[cyan]{sid[:8]}[/cyan]" for sid in sources)
        console.print(
            f"  [yellow]📚[/yellow] [dim]borrowed from past asks:[/dim] {joined} "
            f"[dim](use [cyan]/history show {sources[0][:8]}[/cyan] to inspect)[/dim]"
        )
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
    """In-REPL /model subcommand.

    Subcommands:
        /model                  list models (numbered)
        /model use NAME         switch the default model
        /model rm NAME-or-N     delete one model (asks for confirm)
        /model rm               interactive: walk every model with y/N
    """
    from anthill.core.userconfig import (
        load_config,
        remove_secret,
        save_config,
    )

    rest = rest.strip()
    cfg = load_config()

    def _list() -> None:
        if not cfg.models:
            console.print(
                "[dim]No models configured.[/dim] Run "
                "[cyan]anthill model add[/cyan] or [cyan]/setup[/cyan]."
            )
            return
        for i, m in enumerate(cfg.models, start=1):
            star = "★" if m.name == cfg.default_model else " "
            console.print(
                f"  [cyan]{i}[/cyan] {star} [cyan]{m.name}[/cyan]  "
                f"[dim]{m.provider} / {m.model}[/dim]"
            )
        console.print(
            "  [dim]/model use NAME  ·  /model rm NAME-or-N  ·  "
            "/model rm  (interactive)[/dim]"
        )

    def _resolve(token: str):  # noqa: ANN202
        """Accept either a model name or a 1-based index from the list."""
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(cfg.models):
                return cfg.models[idx]
            return None
        return cfg.find_model(token)

    def _delete_one(entry) -> None:  # noqa: ANN001
        cfg.models = [m for m in cfg.models if m.name != entry.name]
        remove_secret(entry.secret_ref)
        if cfg.default_model == entry.name:
            cfg.default_model = cfg.models[0].name if cfg.models else None
        save_config(cfg)
        console.print(f"  [green]✓[/green] removed [cyan]{entry.name}[/cyan]")

    if not rest or rest == "list":
        _list()
        return

    if rest.startswith("use "):
        target = rest[4:].strip()
        entry = _resolve(target)
        if entry is None:
            console.print(f"[red]No model named or indexed '{target}'.[/red]")
            return
        cfg.default_model = entry.name
        save_config(cfg)
        console.print(f"[green]Default model is now '{entry.name}'.[/green]")
        return

    if rest in ("rm", "remove"):
        # 0.1.17+ — interactive walk. Useful when the user has 4 stale
        # test entries and doesn't want to type each name.
        if not cfg.models:
            console.print("[dim]Nothing to remove.[/dim]")
            return
        console.print(
            "[dim]Interactive removal — y to delete, anything else to keep, "
            "Ctrl+C to stop.[/dim]"
        )
        for entry in list(cfg.models):  # snapshot, _delete_one mutates cfg.models
            star = " ★" if entry.name == cfg.default_model else ""
            try:
                answer = input(
                    f"  remove [cyan]{entry.name}[/cyan]"
                    f"{star}  ({entry.provider}/{entry.model})? [y/N] "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print()
                console.print("  [dim]stopped.[/dim]")
                return
            if answer in ("y", "yes"):
                _delete_one(entry)
        return

    if rest.startswith("rm ") or rest.startswith("remove "):
        # /model rm NAME-or-INDEX with optional --yes for scripted flows.
        parts = rest.split()
        force = "--yes" in parts or "-y" in parts
        targets = [p for p in parts[1:] if p not in ("--yes", "-y")]
        if not targets:
            console.print(
                "[yellow]Usage: /model rm NAME-or-N [more...] | "
                "/model rm (interactive)[/yellow]"
            )
            return
        for token in targets:
            entry = _resolve(token)
            if entry is None:
                console.print(f"[red]No model named or indexed '{token}'.[/red]")
                continue
            if not force:
                try:
                    confirm = input(
                        f"  remove [cyan]{entry.name}[/cyan]? "
                        f"This is permanent. [y/N] "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    return
                if confirm not in ("y", "yes"):
                    console.print("  [dim]skipped.[/dim]")
                    continue
            _delete_one(entry)
        return

    if rest.startswith("test ") or rest == "test":
        # 0.1.24 — verify a model's API key right inside the REPL.
        # The `anthill model test` CLI command already exists; this
        # wraps it so users don't have to leave the session when
        # they're trying to diagnose the (auth) error.
        target = rest[len("test"):].strip()
        if not target:
            console.print("[yellow]Usage: /model test NAME-or-N[/yellow]")
            return
        entry = _resolve(target)
        if entry is None:
            console.print(f"[red]No model named or indexed '{target}'.[/red]")
            return
        from anthill.cli.model_cmd import _probe_model
        from anthill.core.userconfig import load_secrets
        api_key = load_secrets().get(entry.secret_ref)
        if not api_key:
            console.print(
                f"[red]No API key in secrets.toml for '{entry.name}'.[/red]"
            )
            return
        import asyncio
        console.print(f"  Testing [cyan]{entry.name}[/cyan]... ", end="")
        result = asyncio.run(_probe_model(entry, api_key))
        if result["ok"]:
            console.print(
                f"[green]✓ ok[/green] [dim]"
                f"{result['latency_ms']:.0f}ms, "
                f"{result.get('out_tokens', 0)} tokens[/dim]"
            )
        else:
            console.print(f"[red]✗ {result['error']}[/red]")
        return

    console.print(
        "[yellow]Usage: /model | /model use NAME | "
        "/model rm [NAME-or-N] | /model test NAME[/yellow]"
    )


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

    # 0.1.5+ — wire up arrow-key history + line editing + persistent
    # history file before the user types anything. Done BEFORE the
    # splash so even the first prompt benefits from it.
    _setup_readline(config.home)
    # 0.1.14 — Tab completion for slash commands, models, nations, @file.
    from anthill.cli.completion import install_readline_completion
    install_readline_completion()

    console.print()
    _splash_banner(nation, stats)
    console.print()

    # 0.1.20 — proxy preflight. We just hit a real user whose
    # ALL_PROXY=socks5://... made every attempt fail with "socksio
    # not installed" — the symptom looked like a model bug. Catch
    # this BEFORE the first ask burns three retries.
    _proxy_preflight()

    # 0.1.21 — citizen-model preflight. Same shape as proxy_preflight:
    # the actual bug is a citizen pointing at a model name the user no
    # longer has configured ("minimax" left over after the user
    # reconfigured as "deepseek"). Without this warning, every ask
    # burns three retries with "(auth)" errors.
    _citizen_model_preflight(nation)

    # 0.1.5+ — first-run gate. If no model is configured we can't do
    # ANYTHING useful in the REPL; instead of letting the user type
    # into a broken state (and watch every ask fail 3 times), offer
    # to launch the interactive wizard immediately.
    if _no_model_configured():
        console.print(
            "[yellow]⚠ No model configured.[/yellow] "
            "Without a model, every ask will fail."
        )
        console.print(
            "[dim]Launch the interactive setup wizard now?[/dim]"
        )
        try:
            choice = input("  [Y/n] » ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if choice in ("", "y", "yes"):
            from anthill.cli.setup_cmd import run_wizard
            run_wizard(force=False)
            # Reload nation in case setup changed/added a default model.
            refreshed = load_nation(nation_name, config.home)
            if refreshed is not None:
                nation = refreshed
            console.print()
            _print_status_bar(nation, stats)
            console.print()
        else:
            console.print(
                "  [dim]Skipping. You can run [cyan]/setup[/cyan] "
                "any time, or [cyan]/quit[/cyan] to exit.[/dim]"
            )
            console.print()

    # 0.1.6+ — onboarding card. Print only when a model is configured;
    # the no-model path was already handled by the setup gate above.
    if not _no_model_configured():
        _onboarding_card(nation)

    while True:
        try:
            line = _read_request_line(prompt="» ")
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
            elif cmd == "setup":
                # 0.1.5+ — re-enter the wizard on demand. Useful when the
                # user said "n" at startup but changed their mind, or wants
                # to add a second provider.
                from anthill.cli.setup_cmd import run_wizard
                run_wizard(force=False)
                refreshed = load_nation(nation.name, config.home)
                if refreshed is not None:
                    nation = refreshed
            elif cmd in ("citizens", "citizen"):
                # 0.1.21 — repair unresolvable citizens. /citizens
                # alone prints diagnostic; /citizens migrate fixes
                # everything broken; /citizens migrate-all blasts
                # every alive citizen at the default.
                from anthill.core.citizen_check import (
                    find_unresolvable_citizens,
                    migrate_citizens_to,
                )
                from anthill.core.userconfig import load_config

                cfg = load_config()
                configured = [m.name for m in cfg.models]
                action = rest.strip().lower()

                # 0.1.24 — `/citizens migrate FROM` evacuates every
                # citizen on a specific model (used when the user
                # KNOWS one of their configured models has a bad
                # key and wants to move off it). Recognized as
                # "migrate <something-other-than-keyword>".
                migrate_from = None
                if action.startswith("migrate ") and action != "migrate-all":
                    rest_of = action[len("migrate "):].strip()
                    if rest_of and rest_of not in ("from",):
                        migrate_from = rest_of

                if migrate_from is not None:
                    if not cfg.default_model:
                        console.print(
                            "  [yellow]No default model configured.[/yellow]"
                        )
                    else:
                        from anthill.core.citizen_check import (
                            migrate_citizens_from,
                        )
                        n = migrate_citizens_from(
                            nation.agents,
                            from_model=migrate_from,
                            to_model=cfg.default_model,
                        )
                        save_nation(nation, config.home)
                        console.print(
                            f"  [green]✓[/green] migrated {n} citizen(s) "
                            f"from [red]{migrate_from}[/red] "
                            f"to [cyan]{cfg.default_model}[/cyan]"
                        )
                elif action in ("migrate", "fix"):
                    if not cfg.default_model:
                        console.print(
                            "  [yellow]No default model configured. "
                            "Run /setup first.[/yellow]"
                        )
                    else:
                        n = migrate_citizens_to(
                            nation.agents,
                            cfg.default_model,
                            only_unresolvable=True,
                            configured_model_names=configured,
                        )
                        save_nation(nation, config.home)
                        console.print(
                            f"  [green]✓[/green] migrated {n} citizen(s) to "
                            f"[cyan]{cfg.default_model}[/cyan]"
                        )
                        if n == 0:
                            # 0.1.24 — point the user at the third
                            # case when /citizens migrate finds nothing.
                            by_model: dict[str, int] = {}
                            for a in nation.agents:
                                if a.is_retired or a.is_quarantined:
                                    continue
                                by_model[a.model] = by_model.get(a.model, 0) + 1
                            other_models = [
                                m for m in by_model
                                if m != cfg.default_model
                            ]
                            if other_models:
                                console.print(
                                    "  [dim]All citizens are on configured models.[/dim]"
                                )
                                for m in other_models:
                                    console.print(
                                        f"  [dim]If [cyan]{m}[/cyan]'s "
                                        f"key is the problem: "
                                        f"[cyan]/citizens migrate {m}[/cyan][/dim]"
                                    )
                elif action in ("migrate-all", "fix-all"):
                    if not cfg.default_model:
                        console.print(
                            "  [yellow]No default model configured.[/yellow]"
                        )
                    else:
                        n = migrate_citizens_to(
                            nation.agents,
                            cfg.default_model,
                            only_unresolvable=False,
                        )
                        save_nation(nation, config.home)
                        console.print(
                            f"  [green]✓[/green] migrated {n} citizen(s) "
                            f"(all alive) to [cyan]{cfg.default_model}[/cyan]"
                        )
                else:
                    # No-arg / "show" — diagnostic
                    issues = find_unresolvable_citizens(nation.agents, configured)
                    alive = [a for a in nation.agents if not a.is_retired and not a.is_quarantined]
                    console.print(
                        f"  [bold]{len(alive)}[/bold] alive citizen(s) "
                        f"[dim]({sum(1 for a in nation.agents if a.is_retired)} retired)[/dim]"
                    )
                    by_model: dict[str, int] = {}
                    for a in alive:
                        by_model[a.model] = by_model.get(a.model, 0) + 1
                    for m, count in sorted(by_model.items()):
                        broken = m in {i.model for i in issues}
                        tag = "[red](broken)[/red]" if broken else ""
                        console.print(f"    [cyan]{count}×[/cyan] {m} {tag}")
                    if issues:
                        console.print(
                            "  [dim]/citizens migrate     repair broken ones[/dim]"
                        )
                        console.print(
                            "  [dim]/citizens migrate-all  reset ALL to default[/dim]"
                        )
            elif cmd in ("skills", "skill"):
                # 0.1.17 — show what skill_mining sees in this nation's
                # history. Useful for "what does the system think I do
                # a lot?" without waiting for a nudge.
                from anthill.core.skill_mining import mine_skills
                history_now = load_history(nation_dir(config.home, nation.name))
                skills = mine_skills(history_now)
                if not skills:
                    console.print(
                        "  [dim]No recurring patterns yet. "
                        "Ask similar things 3+ times and they'll show up here.[/dim]"
                    )
                else:
                    console.print(
                        f"  [bold]{len(skills)} recurring pattern(s):[/bold]"
                    )
                    for s in skills[:10]:
                        snippet = s.representative.replace("\n", " ")[:70]
                        console.print(
                            f"    [cyan]{s.occurrences}×[/cyan] {snippet}"
                        )
            elif cmd == "project":
                # 0.1.15 — inspect the project context Scout sees.
                from anthill.core.project import (
                    find_project_root,
                    project_context_block,
                )
                _proj = find_project_root()
                if _proj is None:
                    console.print(
                        "  [dim]No project detected at cwd or any parent.[/dim]"
                    )
                else:
                    console.print(f"  [dim]{project_context_block(_proj)}[/dim]")
            elif cmd == "plan":
                # 0.1.13 — toggle plan review. When on, every non-trivial
                # ask gives the user a chance to skip/keep subtasks before
                # execution. Default off — opt-in for the user who wants
                # to micromanage Scout.
                arg = rest.strip().lower()
                if arg in ("on", "yes", "1"):
                    stats.plan_review = True
                elif arg in ("off", "no", "0"):
                    stats.plan_review = False
                else:
                    stats.plan_review = not stats.plan_review
                state = (
                    "[green]on[/green]" if stats.plan_review else "[dim]off[/dim]"
                )
                console.print(
                    f"  Plan review {state} "
                    f"[dim](Scout's plan will be reviewed before execution)[/dim]"
                )
            else:
                console.print(f"[yellow]Unknown command: /{cmd}.[/yellow] Try /help.")
            continue

        # 0.1.5+ — refuse to call into a misconfigured nation. Without
        # this the user types something, every retry burns through every
        # citizen, and they see three angry red ✗ lines for no reason.
        if _no_model_configured():
            console.print(
                "[yellow]✗ No model configured.[/yellow] "
                "Run [cyan]/setup[/cyan] for the wizard, or "
                "[cyan]/quit[/cyan] to exit."
            )
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
