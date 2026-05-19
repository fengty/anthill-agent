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


def _surface_pending_bg_deliveries(nation, config, stats) -> None:  # noqa: ANN001
    """0.1.37 — notify user of background jobs that finished since last prompt.

    Scoped to deliveries originating from THIS REPL session (so a job
    started by `anthill bg ask` from another terminal doesn't pop into
    THIS REPL — it'll surface in whichever surface started it).
    Marks each notified job `delivered.json` so we don't re-notify
    on every prompt tick.

    Best-effort throughout: any filesystem hiccup is swallowed.
    """
    try:
        from anthill.core.background import (
            mark_delivered,
            pending_deliveries,
        )
    except Exception:  # noqa: BLE001
        return
    session = getattr(stats, "session", None)
    session_id = session.session_id if session else ""
    try:
        ndir = nation_dir(config.home, nation.name)
        pending = pending_deliveries(
            ndir,
            origin_surface="repl",
            origin_session_id=session_id,
        )
    except Exception:  # noqa: BLE001
        return
    for job in pending:
        # Pull the FIRST 120 chars of the result for a one-line teaser.
        snippet = ""
        try:
            log_text = job.log_path.read_text(encoding="utf-8")
            # The "final answer" tends to be after the synthesis card
            # but we don't reliably know where; just show last non-
            # blank chunk.
            lines = [ln for ln in log_text.splitlines() if ln.strip()]
            if lines:
                snippet = lines[-1][:120]
        except OSError:
            snippet = ""
        ok = job.status == "completed"
        icon = "✅" if ok else "⚠️"
        req_short = job.request.replace("\n", " ")[:60]
        if len(job.request) > 60:
            req_short += "…"
        console.print(
            f"  {icon} [bold]background job done[/bold] "
            f"[cyan]{job.job_id[:8]}[/cyan]  "
            f"[dim]({job.runtime_seconds:.0f}s)[/dim]"
        )
        console.print(
            f"     [dim]request:[/dim] {req_short}"
        )
        if snippet:
            console.print(f"     [dim]↳ {snippet}[/dim]")
        console.print(
            f"     [dim]View full: [cyan]/bg show {job.job_id[:8]}[/cyan][/dim]"
        )
        mark_delivered(job)


def _prompt_steer_choice(original_request: str) -> "str | None":
    """0.1.36 — interrupt-and-steer menu after Ctrl+C during an ask.

    Returns:
      - ``None``  to mean "user just wants to cancel"
      - non-empty string for a redirect — the new instruction the
        user wants to swap in. Caller frames it as a follow-up
        with the original ask quoted so the model treats it as a
        correction, not a fresh question.

    UX rule (mirrors Hermes + Claude Code): cancellation should be
    the cheap default. Empty input / Ctrl+C / unrecognized choice
    all mean cancel. Redirect requires explicit "r" + a non-empty
    follow-up. Don't trap the user in the menu.
    """
    console.print()
    console.print(
        "  [yellow]⏸  paused.[/yellow] "
        "[dim]Press Enter or [c] to cancel · [r] to redirect with "
        "a new instruction.[/dim]"
    )
    try:
        choice = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice not in ("r", "redirect"):
        return None
    try:
        redirect = input("  redirect: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not redirect:
        return None
    # Surface a confirming line so the user sees we accepted the redirect.
    short = original_request.replace("\n", " ")[:60]
    if len(original_request) > 60:
        short += "…"
    console.print(
        f"  [dim]↪ redirecting from [italic]{short}[/italic] → "
        f"running new instruction…[/dim]"
    )
    return redirect


def _pick_session(metas, config, nation_name):  # noqa: ANN001
    """0.1.35 — interactive picker over recent sessions for THIS nation.

    Shows up to 10 sessions; user types a number to resume one, or
    Enter / 'n' to start a fresh session.
    """
    import time as _time
    from anthill.core.sessions import start_session
    from anthill import __version__ as _av

    console.print()
    console.print("  [bold]Recent sessions[/bold]")
    for i, meta in enumerate(metas, start=1):
        when_ago = _time.time() - meta.last_turn_at
        if when_ago < 3600:
            when = f"{int(when_ago // 60)}m ago"
        elif when_ago < 86400:
            when = f"{int(when_ago // 3600)}h ago"
        else:
            when = f"{int(when_ago // 86400)}d ago"
        head = meta.first_request.replace("\n", " ")[:55]
        if len(meta.first_request) > 55:
            head += "…"
        console.print(
            f"    [cyan]{i}[/cyan]) "
            f"[dim]{meta.session_id[:14]}[/dim]  "
            f"[dim]{when}[/dim]  "
            f"[dim]{meta.turn_count} turn(s)[/dim]  "
            f"{head}"
        )
    console.print(
        "    [dim]Enter to start fresh · 1-N to resume[/dim]"
    )
    try:
        choice = input("  resume? ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ""
    if not choice or choice.lower() in ("n", "no", "new"):
        return start_session(config.home, nation_name, version=_av)
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(metas):
            from anthill.core.sessions import load_session
            sess = load_session(metas[idx].session_id, config.home)
            if sess is not None:
                return sess
    console.print("  [yellow]didn't recognize that — starting fresh.[/yellow]")
    return start_session(config.home, nation_name, version=_av)


def _load_memory_into_nation(nation: Nation, config: AnthillConfig) -> None:
    """0.1.29 — composes USER.md + MEMORY.md into nation.memory_context.

    Called once at REPL start AND after any slash command that
    mutates either file, so the next ask sees the fresh content.
    Tolerates missing files; tolerates write errors. Memory must
    never break the REPL.
    """
    try:
        from anthill.core.memory_files import (
            build_memory_block,
            read_nation_memory,
            read_user_md,
        )
    except Exception:  # noqa: BLE001
        return
    try:
        user_md = read_user_md(config.home)
        nation_md = read_nation_memory(nation_dir(config.home, nation.name))
        nation.memory_context = build_memory_block(user_md, nation_md)
    except Exception:  # noqa: BLE001 — memory injection is best-effort
        nation.memory_context = ""


def _edit_in_external_editor(path: Path) -> bool:
    """Spawn $EDITOR (falls back to nano / vi) on ``path``.

    Returns True if the editor exited cleanly. We deliberately don't
    parse the result — the file is the source of truth, the editor
    is just a UX convenience.
    """
    import os
    import shutil
    import subprocess

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        for candidate in ("nano", "vim", "vi"):
            if shutil.which(candidate):
                editor = candidate
                break
    if not editor:
        console.print(
            "[yellow]No $EDITOR set and no nano/vi found. "
            "Edit the file by hand:[/yellow]"
        )
        console.print(f"  [cyan]{path}[/cyan]")
        return False
    try:
        subprocess.run([editor, str(path)], check=False)
        return True
    except OSError as e:
        console.print(f"[red]Could not launch editor: {e}[/red]")
        return False


# 0.1.32 — module-level cache for the most recent batch of pending
# user-model inferences. Stays in memory across slash invocations
# in the same REPL session; cleared once the user accepts or skips.
_PENDING_INFERENCES: list = []


def _user_model_preflight(nation: Nation, config: AnthillConfig) -> None:
    """Run user_model.infer_user_model() at session start.

    Quiet when no high-confidence inferences exist OR when every
    inference is already noted in USER.md. Otherwise prints a short
    "🔍 noticed about you" block and stores the pending inferences
    so `/profile accept` (or `/profile accept <kind>`) can write
    them.

    Best-effort: any exception is swallowed. Memory must never
    block the REPL.
    """
    global _PENDING_INFERENCES
    try:
        from anthill.core.feedback import load_exemplars
        from anthill.core.history import load_history
        from anthill.core.memory_files import read_user_md
        from anthill.core.user_model import already_recorded, infer_user_model

        ndir = nation_dir(config.home, nation.name)
        history = load_history(ndir, limit=DEFAULT_INFER_WINDOW)
        exemplars = load_exemplars(ndir)
        user_md = read_user_md(config.home)

        inferences = infer_user_model(history, exemplars)
        # Drop any already in USER.md so we don't pester the user
        # every session about the same finding.
        inferences = [i for i in inferences if not already_recorded(i, user_md)]
    except Exception:  # noqa: BLE001
        return

    _PENDING_INFERENCES = list(inferences)
    if not inferences:
        return
    console.print(
        "[bold]🔍 noticed about you[/bold] "
        "[dim](review with [cyan]/profile accept[/cyan])[/dim]"
    )
    for inf in inferences:
        pct = int(inf.confidence * 100)
        console.print(
            f"  [dim]·[/dim] [cyan]{inf.kind}[/cyan] "
            f"[dim]({pct}% confidence)[/dim]  {inf.summary}"
        )
    console.print()


# How much history user_model.infer_user_model uses by default. Mirrored
# from core/user_model.DEFAULT_WINDOW; kept here so the preflight import
# stays cheap (no module-level import of user_model needed).
DEFAULT_INFER_WINDOW = 30


def _accept_inferences(
    nation: Nation,
    config: AnthillConfig,
    target_kind: str | None = None,
) -> None:
    """Write pending inferences to USER.md. ``target_kind`` lets the
    user accept a subset; default is "all pending."

    Each accepted line ends with an HTML comment marker
    ``<!-- auto:<kind> -->`` so the next session's preflight can dedup
    by kind. Lines stay plain-text editable; the comment doesn't
    render visibly when the file is read by humans.
    """
    global _PENDING_INFERENCES
    if not _PENDING_INFERENCES:
        console.print("  [dim]No pending inferences.[/dim]")
        return

    from anthill.core.memory_files import append_user_md

    written = 0
    skipped = 0
    new_pending = []
    for inf in _PENDING_INFERENCES:
        if target_kind is not None and inf.kind != target_kind:
            new_pending.append(inf)
            skipped += 1
            continue
        # Marker pairs with already_recorded() dedup.
        line = f"{inf.summary}  <!-- auto:{inf.kind} -->"
        ok = append_user_md(
            config.home, line, section=inf.suggested_section,
        )
        if ok:
            written += 1
        else:
            new_pending.append(inf)
    _PENDING_INFERENCES = new_pending
    if written:
        console.print(
            f"  [green]✓[/green] accepted {written} inference(s) "
            f"into [cyan]USER.md[/cyan]"
        )
        _load_memory_into_nation(nation, config)
    if skipped and target_kind is not None:
        console.print(
            f"  [dim]({skipped} other pending — accept with "
            f"/profile accept or /profile accept <kind>)[/dim]"
        )


def _skip_inferences(target_kind: str | None = None) -> None:
    """Forget the current pending inferences without writing them."""
    global _PENDING_INFERENCES
    if not _PENDING_INFERENCES:
        console.print("  [dim]Nothing to skip.[/dim]")
        return
    if target_kind is None:
        n = len(_PENDING_INFERENCES)
        _PENDING_INFERENCES = []
        console.print(f"  [dim]skipped {n} inference(s) for this session[/dim]")
    else:
        before = len(_PENDING_INFERENCES)
        _PENDING_INFERENCES = [i for i in _PENDING_INFERENCES if i.kind != target_kind]
        diff = before - len(_PENDING_INFERENCES)
        console.print(f"  [dim]skipped {diff} inference(s) for kind={target_kind}[/dim]")


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
    /timing       per-task_type + per-phase latency over this session
    /project      project context Scout sees (cwd, git branch, files)
    /skills       recurring patterns the nation has noticed in history
    /skill save X distill the last complex ask into a named skill
                  (next similar ask uses it automatically)
    /skill list   show all saved skills (with usage stats)
    /skill rm X   remove a specific saved skill
    /skill prune  remove all 🌫 stale skills (>14d unused)
    /memory       this nation's persistent MEMORY.md
    /memory consolidate
                  dedup near-duplicates, archive overflow
    /profile      your global USER.md (alias: /preferences)
    /profile consolidate
                  same hygiene pass for USER.md
    /profile accept [kind]
                  commit inferences the nation has noticed about you
    /profile skip [kind]
                  dismiss pending inferences for this session
    /remember X   add a one-line lesson to MEMORY.md
    /remember-me X
                  add a one-line fact about yourself to USER.md
    /recall X     full-text search every past ask in this nation
    /session      info about current + recent saved sessions
                  (resume from outside with: anthill --resume <id>)
    /bg ask X     fire an ask in the background; result returns here
                  automatically when done. /bg list and /bg show <id>
                  for the rest.

  [bold]Mid-ask controls (0.1.36+)[/bold]
    Ctrl+C        pause the current ask → [c]ancel · [r]edirect with new instruction
                  Picks up the agent's work without killing the REPL.
    /citizens          list alive citizens + which models they use
    /citizens migrate  point all unresolvable citizens at the default
    /citizens migrate X
                       evacuate every citizen running on model X
                       (use when X is configured but its key is bad)

  [bold]Steer[/bold]
    /rate up      strengthen pheromones for the last answer
    /rate down    erode pheromones for the last answer
    /model         list configured models (numbered)
    /model add     interactive — add a new model (same flow as /setup)
    /model use X   switch default model (X = name or list index)
    /model rm X    delete a model (X = name or index)
    /model rm      interactive — walk each model with y/N
    /model test X  verify a model's API key (the (auth) diagnostic)
    /nation X     switch to a different nation (creates if missing)
    /plan         toggle plan review (skip/keep subtasks before run)
    /setup        relaunch the interactive setup wizard
    /setup browser  install Playwright + chromium for URL fetch fallback
                    (one-shot; only needed if you paste SPA / login-walled URLs)

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
        # 0.1.33 — project-context injection mode. "auto" (default)
        # lets is_project_relevant_request decide per-ask. "on" forces
        # injection. "off" disables it for the session. Toggle via
        # /project on / off / auto.
        self.project_inject_mode: str = "auto"
        # 0.1.28 — in-session conversation memory. Real user hit:
        # "» 最近热门电影 → answer / » 我说的是 2026 年的 → 'what 2026
        # topic?'" — citizens had no idea what the user just said
        # one turn ago. This rolling window of recent (req, resp)
        # tuples is injected into prompts when the next ask looks
        # like a follow-up.
        from anthill.core.conversation import ConversationContext
        self.conversation = ConversationContext()
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
    # 0.1.29 — surface persistent memory at session start. If either
    # USER.md or MEMORY.md has content, show "📓 N memory lines · M
    # about you" so the user knows the nation has carried state across
    # restarts (the core "越用越聪明" signal).
    try:
        from anthill.config import AnthillConfig as _Cfg
        from anthill.core.memory_files import (
            line_count as _mem_lines,
            read_nation_memory,
            read_user_md,
        )
        from anthill.core.persistence import nation_dir as _ndir
        _home = _Cfg.load().home
        _user_lines = _mem_lines(read_user_md(_home))
        _nation_lines = _mem_lines(read_nation_memory(_ndir(_home, nation.name)))
    except Exception:  # noqa: BLE001
        _user_lines = _nation_lines = 0
    if _user_lines or _nation_lines:
        stats_table.add_row(
            "memory",
            f"📓 [bold]{_nation_lines}[/bold] nation lines · "
            f"[bold]{_user_lines}[/bold] about you",
        )

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
        # 0.1.58 — curator-lite: passive stale-skill nudge at REPL start.
        # If there are 3+ saved skills that have been unused for 14+
        # days, hint at /skill prune. We never auto-delete (that's the
        # 0.1.51 explicit command's job) — this is just visibility. The
        # check is best-effort: a recipe load failure must not crash
        # the splash, so wrap everything. nation_dir is derived from
        # nation.history_path.parent so we don't need the AnthillConfig
        # here.
        try:
            from anthill.core.recipes import list_recipes
            from anthill.core.skill_stats import partition_stale
            ndir = (
                nation.history_path.parent
                if nation.history_path is not None
                else None
            )
            if ndir is not None and ndir.exists():
                stale, _keep = partition_stale(list_recipes(ndir))
                if len(stale) >= 3:
                    names = ", ".join(r.name for r in stale[:3])
                    if len(stale) > 3:
                        names += f" (+{len(stale) - 3} more)"
                    console.print(
                        f"  [dim]🌫 {len(stale)} skill(s) unused 14d+: "
                        f"{names} — try [cyan]/skill prune[/cyan][/dim]"
                    )
        except Exception:  # noqa: BLE001 — passive nudge must not crash REPL
            pass
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

    # 0.1.33 — sync the session-level project-injection mode into the
    # nation so Nation.ask sees the user's current toggle preference.
    nation.project_inject_mode = getattr(stats, "project_inject_mode", "auto")

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

    # 0.1.38 — URL auto-attachment. When the user pastes a http(s)
    # URL in their request, fetch it BEFORE Scout sees the prompt
    # and inline the readable text. Mirrors @file (0.1.11) but for
    # URLs. Closes the real-user gap "贴了 URL，Anthill 说我不会
    # 浏览网页." Login-walled pages are detected and skipped with
    # a friendly hint instead of feeding HTML into the prompt.
    # 0.1.53 — track URL fetch outcome so the clarifier guard below
    # can react: when the user posted a URL but we couldn't actually
    # see its content (login wall, thin content), bouncing back
    # through clarifier "请把页面内容粘贴过来" is exactly the
    # citizens-serve-the-king violation we keep fighting. Let the
    # subtask run, fail/refuse gracefully, and the 0.1.40 retry
    # path can do its thing. Don't add another round-trip first.
    url_fetch_was_skipped = False
    try:
        from anthill.core.url_attachments import expand_urls
        url_block = expand_urls(request)
        if url_block.fetched:
            hosts = ", ".join(f.display_host for f in url_block.fetched[:3])
            if len(url_block.fetched) > 3:
                hosts += f" (+{len(url_block.fetched) - 3} more)"
            total_kb = sum(f.char_count for f in url_block.fetched) / 1024
            # 0.1.54 — flag browser-rescued URLs so user knows
            # the Playwright fallback fired (and why the fetch
            # took longer than usual).
            browser_count = sum(
                1 for f in url_block.fetched if getattr(f, "via_browser", False)
            )
            browser_tag = (
                f" [via 🌐 browser ×{browser_count}]" if browser_count else ""
            )
            console.print(
                f"  [dim]🔗 fetched {len(url_block.fetched)} URL(s): "
                f"{hosts} · {total_kb:.1f} KB{browser_tag}[/dim]"
            )
        for err in url_block.errors:
            console.print(
                f"  [yellow]⚠ skipped {err.url}[/yellow] "
                f"[dim]({err.reason})[/dim]"
            )
            url_fetch_was_skipped = True
        # Block rendered text goes IN FRONT of any @file block and
        # in front of the user request, so Scout sees the fetched
        # context before the question.
        rendered = url_block.render()
        if rendered:
            effective_request = rendered + effective_request
    except Exception:  # noqa: BLE001 — URL fetch must never break the REPL
        pass

    # 0.1.28 — conversation memory injection. When the current ask
    # looks like a follow-up ("我说的是 2026 年的", "tell me more",
    # short ambiguous fragment after a real ask), prepend the recent
    # turn(s) to the prompt that reaches Scout so the planner sees
    # what the user is actually continuing.
    from anthill.core.conversation import is_follow_up, wrap_with_context
    last_turn = stats.conversation.last_turn()
    if is_follow_up(request, last_turn):
        effective_request = wrap_with_context(
            effective_request, stats.conversation.recent()
        )
        # Visible signal so the user knows context is being carried.
        n_turns = len(stats.conversation)
        console.print(
            f"  [dim]↳ continuing from {n_turns} previous turn(s)[/dim]"
        )

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
            # 0.1.41 — user-serving refusals get a friendlier UI than
            # the generic "retry attempt N failed ([red]<error>[/red])"
            # treatment. The refusal body was just streamed in full
            # via on_token; the 100-char snippet would be redundant
            # noise. Show a clear "the citizen deferred — retrying
            # with a resourceful nudge" line instead, and skip the
            # err_blurb entirely for this failure class.
            if reason == "user_serving_refusal":
                console.print(
                    f"    [yellow]🛠 attempt {event.attempt_number} "
                    f"deferred[/yellow] [dim]— citizen punted "
                    f"the work back; retrying with a "
                    f"resourceful nudge…[/dim]"
                )
            else:
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

        # 0.1.27 — live deliberation phase indicator. Between rounds
        # we used to go silent for 5-15s while the critique LLM
        # produced a critique, then suddenly fire round 2. Now the
        # user sees: "🔍 critiquing → tokens streaming in real time
        # → ✓ critique done → ✍ refining (round 2)". Same closure
        # state the streaming-token handler uses, so output stays
        # clean across phase transitions.
        critique_state = {"open": False, "chars": 0}

        def _close_critique_line() -> None:
            if critique_state["open"]:
                console.print()
                critique_state["open"] = False
                critique_state["chars"] = 0

        async def _on_critique_token(delta: str) -> None:
            if not delta:
                return
            if not critique_state["open"]:
                console.print("    [dim magenta]✎[/dim magenta] ", end="")
                critique_state["open"] = True
                critique_state["chars"] = 0
            for piece in delta.splitlines(keepends=True):
                line = piece.rstrip("\n")
                ends_with_nl = piece.endswith("\n")
                if line:
                    console.print(f"[dim magenta]{line}[/dim magenta]", end="")
                    critique_state["chars"] += len(line)
                if ends_with_nl or critique_state["chars"] >= 80:
                    console.print()
                    critique_state["chars"] = 0
                    critique_state["open"] = False
                    if piece is not delta.splitlines(keepends=True)[-1]:
                        console.print("    [dim magenta]✎[/dim magenta] ", end="")
                        critique_state["open"] = True

        async def _on_phase(name: str, payload: dict) -> None:  # noqa: ANN001
            _close_critique_line()
            if name == "critique_start":
                weakest = payload.get("weakest") or {}
                if weakest:
                    weak_summary = ", ".join(
                        f"{k}={v:.2f}" for k, v in sorted(weakest.items(), key=lambda kv: kv[1])
                    )
                    console.print(
                        f"  [bold magenta]🔍 critiquing[/bold magenta] "
                        f"[dim](weakest dims: {weak_summary})[/dim]"
                    )
                else:
                    console.print("  [bold magenta]🔍 critiquing[/bold magenta]")
            elif name == "critique_done":
                # The streaming token handler already rendered the
                # critique body inline. Just close cleanly with
                # the critic id so the user knows who said it.
                critic_id = payload.get("critic_id")
                if critic_id:
                    console.print(
                        f"  [dim magenta]✓ critique by [/dim magenta]"
                        f"[cyan]{critic_id[:12]}[/cyan]"
                    )
            elif name == "refine_start":
                round_n = payload.get("round", "?")
                console.print(
                    f"  [bold magenta]✍ refining[/bold magenta] "
                    f"[dim](round {round_n})[/dim]"
                )

        # 0.1.53 — suppress clarifier when URL fetch was skipped.
        # See the matching block on the regular nation.ask path below.
        clarify_for_this_ask = None if url_fetch_was_skipped else on_clarify
        delib = await run_deliberate(
            nation, effective_request,
            max_rounds=max_rounds,
            quality_threshold=quality_threshold,
            on_progress=on_progress,
            on_clarify=clarify_for_this_ask,  # v0.9.0; 0.1.53 guard
            on_plan=on_plan if stats.plan_review else None,  # v0.1.13
            nation_dir=nation_dir(config.home, nation.name),
            on_round=_on_round,
            on_phase=_on_phase,                 # v0.1.27
            on_critique_token=_on_critique_token,  # v0.1.27
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
        # 0.1.53 — if the URL fetch step couldn't actually retrieve
        # the page (login wall / thin content), the clarifier's
        # "请把页面内容粘贴过来" prompt is exactly the bounce-back
        # we keep telling citizens not to do. The user already
        # KNOWS the fetch failed (⚠ line above). Let the subtask
        # run with what's available; refusal-retry (0.1.40) handles
        # any "I can't do this" output from the citizen path.
        clarify_for_this_ask = None if url_fetch_was_skipped else on_clarify
        result = await nation.ask(
            effective_request,
            on_progress=on_progress,
            on_clarify=clarify_for_this_ask,  # v0.9.0; 0.1.53 guard
            on_plan=on_plan if stats.plan_review else None,  # v0.1.13
            nation_dir=nation_dir(config.home, nation.name),
        )
    save_nation(nation, config.home)

    # 0.1.42 — surface skill match if Nation.ask used a saved recipe
    # instead of letting Scout regenerate the plan. Shown AFTER
    # execution so the user sees both the match line and the
    # finished output without ordering confusion. The match check
    # uses last_matched_skill which gets set in nation.ask only when
    # a skill bypassed Scout.
    matched = getattr(nation, "last_matched_skill", None)
    if matched is not None:
        console.print(
            f"  [dim]📚 used skill [cyan]{matched.recipe.name}[/cyan] "
            f"({int(matched.confidence * 100)}% match via "
            f"{matched.matched_via})[/dim]"
        )
        # Clear the marker so the next ask doesn't re-print.
        nation.last_matched_skill = None

    # 0.1.44 — per-ask timing breakdown. Always print: this is the
    # only way for the user to tell WHY an ask took N seconds —
    # Scout, a slow subtask, refusal-retry, or wall-clock dominated
    # by something else. Compact one-liner; defers cluttering output
    # to the session JSONL for post-hoc analysis. Format examples:
    #   [2.3s (trivial)]
    #   [14.8s — Scout 3.1s · research 6.4s · analyze 5.3s]
    #   [103s — Scout 4.2s · research 41.0s · analyze 58.4s · 1 refusal-retry]
    #   [9.0s (skill) — research 4.1s · analyze 4.9s]
    timings = getattr(result, "timings", None)
    if timings is not None and timings.total_seconds > 0:
        parts: list[str] = []
        # 0.1.47 — show clarify before Scout. If it dominated total,
        # user immediately sees "the clarifier ate 7s, not the work".
        if getattr(timings, "clarify_seconds", None) is not None:
            parts.append(f"Clarify {timings.clarify_seconds:.1f}s")
        if timings.scout_seconds is not None:
            parts.append(f"Scout {timings.scout_seconds:.1f}s")
        for tt, secs in timings.subtask_seconds:
            parts.append(f"{tt} {secs:.1f}s")
        if timings.refusal_retry_count > 0:
            parts.append(
                f"{timings.refusal_retry_count} refusal-retry"
            )
        source_tag = (
            f" ({timings.plan_source})"
            if timings.plan_source != "scout"
            else ""
        )
        body = (" — " + " · ".join(parts)) if parts else ""
        console.print(
            f"  [dim]\\[{timings.total_seconds:.1f}s{source_tag}{body}][/dim]"
        )

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
    # 0.1.28 — record this turn in the rolling conversation window so
    # follow-up asks within the same session can reach the recent
    # exchange. Keyed on the VISIBLE request (not effective_request)
    # so the next turn's wrapper doesn't re-wrap our own wrapper.
    final_output = ""
    for outcome in result.outcomes:
        if outcome.status == "ok" and outcome.final is not None:
            final_output = str(outcome.final.output)
    if final_output:
        stats.conversation.record(request, final_output, timestamp=time.time())

    # 0.1.35 — persist this turn to the session JSONL so the next
    # `anthill --resume` can pick up the thread. In-memory window
    # above is for THIS process; the JSONL is for cross-session
    # continuity. Wrapped in try/except: persistence must never
    # break the post-ask path.
    try:
        from anthill.core.sessions import SessionTurn
        session = getattr(stats, "session", None)
        if session is not None and final_output:
            # Keep `duration` as before (sum of attempt durations) so
            # older session readers/tests still see a meaningful value.
            duration = sum(
                a.duration_seconds
                for o in result.outcomes
                for a in o.attempts
            )
            # 0.1.44 — attach the per-phase breakdown as a separate
            # optional field. Forward-compatible: a v0.1.43 reader
            # ignores it.
            t = getattr(result, "timings", None)
            timings_dict = t.to_dict() if t is not None else {}
            session.append_turn(
                SessionTurn(
                    ts=time.time(),
                    request=request,
                    final_output=final_output,
                    plan=[
                        {"task_type": s.task_type, "depends_on": list(s.depends_on)}
                        for s in result.plan.subtasks
                    ],
                    outcomes_summary=[
                        {"status": o.status, "task_type": o.subtask.task_type}
                        for o in result.outcomes
                    ],
                    duration_seconds=duration,
                    timings=timings_dict,
                )
            )
    except Exception:  # noqa: BLE001 — session persistence is best-effort
        pass

    # 0.1.31 — incremental recall index update. Adds the just-finished
    # ask to the FTS5 index so it's immediately searchable next turn
    # AND across future sessions. Wrapped in try/except — recall is
    # best-effort, must not break the post-ask path.
    try:
        from anthill.core.history import HistoryEntry
        from anthill.core.recall import ensure_index
        # Re-derive the entry we just appended so the row's id /
        # timestamp / chain hash line up with what's on disk.
        entry = HistoryEntry(
            id=HistoryEntry.make_id(request, time.time()),
            timestamp=time.time(),
            request=request,
            plan=[{"task_type": s.task_type, "depends_on": s.depends_on}
                  for s in result.plan.subtasks],
            outcomes=[
                {
                    "status": o.status,
                    "output": str(o.final.output) if o.final is not None else "",
                }
                for o in result.outcomes
            ],
        )
        idx = ensure_index(nation_dir(config.home, nation.name))
        if idx is not None:
            idx.index_entry(entry)
            idx.close()
    except Exception:  # noqa: BLE001 — recall is best-effort
        pass

    # 0.1.30 — auto-memory: scan the user's REQUEST for explicit
    # "remember this" / "I prefer X" / "我是 Y" / "我们用 Z" signals.
    # When one fires, append to USER.md or MEMORY.md immediately so
    # the NEXT ask already sees it. Visible via a 📝 line so nothing
    # gets saved invisibly.
    try:
        from anthill.core.auto_memory import (
            TARGET_NATION,
            TARGET_USER,
            extract_memory_signals,
        )
        from anthill.core.memory_files import (
            append_nation_memory,
            append_user_md,
        )
        signals = extract_memory_signals(request)
        if signals:
            for sig in signals:
                if sig.target == TARGET_USER:
                    append_user_md(
                        config.home, sig.content, section=sig.section
                    )
                    where = f"USER.md / {sig.section}"
                elif sig.target == TARGET_NATION:
                    append_nation_memory(
                        nation_dir(config.home, nation.name),
                        sig.content,
                        nation_name=nation.name,
                        section=sig.section,
                    )
                    where = f"MEMORY.md / {sig.section}"
                else:
                    continue
                console.print(
                    f"  [dim]📝 noted in {where}:[/dim] "
                    f"[cyan]{sig.content}[/cyan]"
                )
            # Refresh injected context so the next ask sees this turn.
            _load_memory_into_nation(nation, config)
    except Exception:  # noqa: BLE001 — auto-memory is best-effort
        pass

    # 0.1.17 — skill auto-mining hint. After history is appended,
    # scan for clusters of similar past asks and nudge the user once
    # per session per cluster if the current request belongs to one
    # with ≥3 occurrences.
    #
    # 0.1.45 — apply `worth_saving_as_skill` filter so "你好"×3
    # never triggers the suggestion. Mining detects the *pattern*;
    # judgment decides whether it's a *skill*. Same filter used by
    # auto-save below, so the two never disagree (no more "we
    # suggest you save 你好 but silently refuse to auto-save it").
    try:
        from anthill.core.skill_match import worth_saving_as_skill
        from anthill.core.skill_mining import looks_like_new_match, mine_skills
        history_now = load_history(nation_dir(config.home, nation.name))
        for skill in mine_skills(history_now):
            cluster_key = skill.entry_ids[0]
            if cluster_key in stats.suggested_skill_ids:
                continue
            if not looks_like_new_match(skill, request):
                continue
            verdict, _reason = worth_saving_as_skill(
                skill.representative,
                plan_subtasks=result.plan.subtasks,
            )
            if not verdict:
                # Pattern repeats but isn't skill-worthy. Mark it
                # suggested so we don't re-evaluate every turn —
                # the filter result for "你好" doesn't change with
                # repetition count.
                stats.suggested_skill_ids.add(cluster_key)
                continue
            stats.suggested_skill_ids.add(cluster_key)
            snippet = skill.representative.replace("\n", " ")[:60]
            console.print(
                f"  [dim]💡 you've asked things like '{snippet}…' "
                f"{skill.occurrences} times. Run "
                f"[cyan]/skill save <name>[/cyan] to bake a skill.[/dim]"
            )
            break  # one hint per ask is enough
    except Exception:  # noqa: BLE001 — mining is best-effort, never break the REPL
        pass

    # 0.1.43 — post-success AUTO-distillation. When a complex ask
    # completes successfully WITHOUT a saved skill matching it AND
    # the citizens had to retry-after-refusal (meaning they figured
    # out a NEW resourceful approach), automatically save the recipe.
    # No "/skill save X" prompt — the king shouldn't have to file
    # paperwork. The slug is auto-generated; user can rename via
    # /skill rename later. Closes the user's correction loop end-to-
    # end: 接到任务 → 没现成 skill → 子民做出来 → 自动沉淀 → 下次直接用.
    try:
        from anthill.core.recipes import (
            Recipe,
            RecipeSubtask,
            list_recipes,
            save_recipe,
        )
        from anthill.core.skill_match import (
            distill_request_to_recipe_fields,
            find_matching_skill,
            worth_saving_as_skill,
        )
        ndir = nation_dir(config.home, nation.name)
        had_refusal_retry = any(
            (a.failure_reason == "user_serving_refusal")
            for o in result.outcomes
            for a in o.attempts
        )
        already_skill = find_matching_skill(request, ndir) is not None
        # 0.1.45/46 — single filter for "is this a skill?". Was
        # implicit before via inline conditions; now explicit + shared
        # with the mining hint above so the two paths can't disagree.
        # 0.1.46 adds final_output so the output-structure signal can
        # fire on report-shaped deliverables.
        verdict, save_reason = worth_saving_as_skill(
            request,
            plan_subtasks=result.plan.subtasks,
            had_refusal_retry=had_refusal_retry,
            final_output=result.final_output,
        )
        plan_size = len(result.plan.subtasks)
        if (
            verdict
            and not already_skill
            and result.final_output
        ):
            existing = [r.name for r in list_recipes(ndir)]
            slug, template, description, sub_tuples = (
                distill_request_to_recipe_fields(
                    request,
                    [(s.task_type, s.prompt, s.depends_on) for s in result.plan.subtasks],
                    existing,
                )
            )
            recipe = Recipe(
                name=slug,
                template=template,
                description=description,
                subtasks=[
                    RecipeSubtask(task_type=tt, prompt_template=pt, depends_on=deps)
                    for tt, pt, deps in sub_tuples
                ],
            )
            try:
                save_recipe(recipe, ndir)
                # 0.1.46 — surface WHY we saved this one. The score
                # reason ("score 2.5: refusal_retry + diversity") lets
                # the user calibrate trust in auto-save without
                # opening source.
                console.print(
                    f"  [dim]💾 saved skill [cyan]{slug}[/cyan] "
                    f"({plan_size} subtask(s)) — {save_reason}[/dim]"
                )
            except Exception:  # noqa: BLE001 — auto-save must not break the REPL
                pass
    except Exception:  # noqa: BLE001
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


def _show_timing(
    config: AnthillConfig, nation: Nation, stats: SessionStats
) -> None:
    """0.1.52 — `/timing` aggregates from the current session's JSONL.

    Walks the session file (if any) and renders per-task_type +
    per-phase median latencies. Surfaces slow task_types so the user
    can decide whether to /model assign a faster model to that role.
    """
    from anthill.core.timing_stats import format_summary, summarize_timings

    session = getattr(stats, "session", None)
    if session is None or not session.path.exists():
        console.print(
            "[dim]No session log yet — start asking and timing will accumulate."
            "[/dim]"
        )
        return
    # Pull raw turn dicts straight from the JSONL so we read the
    # exact same shape that was written (no SessionTurn coercion
    # losing fields we don't model).
    import json as _json
    turn_dicts: list[dict] = []
    try:
        with session.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("kind") == "turn":
                    turn_dicts.append(rec)
    except OSError as e:
        console.print(f"[red]could not read session log: {e}[/red]")
        return

    summary = summarize_timings(turn_dicts)
    for line in format_summary(summary):
        console.print(line)
    if summary.by_task_type and any(s.is_slow for s in summary.by_task_type):
        console.print(
            "  [dim]💡 slow task_type? try [cyan]/model[/cyan] to "
            "assign a faster model for that role.[/dim]"
        )


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
            "  [dim]/model add  ·  /model use NAME  ·  /model rm NAME-or-N  ·  "
            "/model test NAME[/dim]"
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
            # 0.1.25 — input() can't render rich markup; print the
            # prompt via console.print then read with a bare input("").
            console.print(
                f"  remove [cyan]{entry.name}[/cyan]{star}"
                f"  [dim]({entry.provider}/{entry.model})[/dim]? [y/N] ",
                end="",
            )
            try:
                answer = input("").strip().lower()
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
                # 0.1.25 — same render-then-read split for the
                # single-target rm path.
                console.print(
                    f"  remove [cyan]{entry.name}[/cyan]? "
                    f"[dim]This is permanent.[/dim] [y/N] ",
                    end="",
                )
                try:
                    confirm = input("").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    return
                if confirm not in ("y", "yes"):
                    console.print("  [dim]skipped.[/dim]")
                    continue
            _delete_one(entry)
        return

    if rest in ("add", "new"):
        # 0.1.25 — interactive add inside the REPL. Reuses the same
        # _add_model_interactive helper the setup wizard calls, so
        # the flow (provider picker → save-as → model id picker →
        # secret prompt) is identical and stays in sync. Solves
        # "I want to add the deepseek key but don't want to leave
        # the REPL to run `anthill model add`."
        from anthill.cli.setup_cmd import _add_model_interactive, _is_tty

        if not _is_tty():
            console.print(
                "[yellow]/model add needs an interactive terminal.[/yellow]"
            )
            return
        try:
            name, _secret_ref = _add_model_interactive(cfg)
        except KeyboardInterrupt:
            console.print()
            console.print("  [dim]cancelled.[/dim]")
            return
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            return
        console.print(f"  [green]✓[/green] added [cyan]{name}[/cyan]")
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
        "[yellow]Usage: /model | /model add | /model use NAME | "
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


def run_repl(
    nation_name: str = "default",
    *,
    resume_session_id: str | None = None,
    force_new_session: bool = False,
) -> int:
    """Drop into the REPL loop. Returns process exit code.

    0.1.35 — session lifecycle:
      - ``resume_session_id="__pick__"`` shows the session picker
      - ``resume_session_id="sess-abc12345"`` (or unique prefix)
        reopens that session directly
      - ``force_new_session=True`` ALWAYS starts a fresh session
      - neither flag = continue most-recent session if its last turn
        is within 24h, else fresh.
    """
    config = AnthillConfig.load()
    config.ensure_home()
    nation = _ensure_nation(config, nation_name)
    stats = SessionStats()

    # 0.1.35 — resolve which session to use (resume vs new). The
    # rolling-window 0.1.28 ConversationContext gets hydrated from
    # the resumed file so the user picks up the thread instead of
    # starting empty.
    from anthill import __version__ as _av
    from anthill.core.sessions import (
        Session,
        list_sessions,
        load_session,
        most_recent_session,
        start_session,
    )

    session: Session | None = None
    if force_new_session:
        session = start_session(config.home, nation_name, version=_av)
    elif resume_session_id == "__pick__":
        # Picker UI — show recent sessions for THIS nation.
        metas = list_sessions(config.home, limit=10, nation_name=nation_name)
        if not metas:
            console.print("[dim]No saved sessions yet — starting fresh.[/dim]")
            session = start_session(config.home, nation_name, version=_av)
        else:
            session = _pick_session(metas, config, nation_name)
    elif resume_session_id is not None:
        session = load_session(resume_session_id, config.home)
        if session is None:
            console.print(
                f"[yellow]No session matches '{resume_session_id}'. "
                f"Starting fresh.[/yellow]"
            )
            session = start_session(config.home, nation_name, version=_av)
    else:
        # No flag: continue if warm (< 24h), else fresh. Mirrors
        # Hermes's default 1440-minute idle policy.
        warm = most_recent_session(config.home, nation_name)
        if warm is not None:
            session = warm
        else:
            session = start_session(config.home, nation_name, version=_av)

    stats.session = session  # stash on stats for the post-ask append point

    # Hydrate the in-memory conversation window from the persisted
    # turns. The window cap (DEFAULT_MAXLEN=4) means we only inject
    # the LAST 4 turns into prompts — older ones stay in the file
    # for /recall to find via FTS5 (0.1.31).
    if session.turns:
        for turn in session.turns:
            if turn.request and turn.final_output:
                stats.conversation.record(
                    turn.request, turn.final_output, timestamp=turn.ts,
                )
        n = min(len(session.turns), 4)
        console.print(
            f"  [dim]↻ resumed session [cyan]{session.session_id}[/cyan]"
            f" — {len(session.turns)} turn(s) total, "
            f"last {n} loaded as context[/dim]"
        )

    # 0.1.5+ — wire up arrow-key history + line editing + persistent
    # history file before the user types anything. Done BEFORE the
    # splash so even the first prompt benefits from it.
    _setup_readline(config.home)
    # 0.1.14 — Tab completion for slash commands, models, nations, @file.
    from anthill.cli.completion import install_readline_completion
    install_readline_completion()

    # 0.1.29 — load USER.md + MEMORY.md and inject as nation.memory_context
    # so every Scout call and every worker call sees them in the system
    # prompt. This is the "I know you" foundation.
    _load_memory_into_nation(nation, config)

    console.print()
    _splash_banner(nation, stats)
    console.print()

    # 0.1.20 — proxy preflight. We just hit a real user whose
    # ALL_PROXY=socks5://... made every attempt fail with "socksio
    # not installed" — the symptom looked like a model bug. Catch
    # this BEFORE the first ask burns three retries.
    _proxy_preflight()

    # 0.1.32 — user-model inference. Look at recent history + rated
    # exemplars and surface any high-confidence preferences the
    # nation noticed. The user accepts with `/profile accept` (added
    # alongside) — we never silent-write inferred lines.
    _user_model_preflight(nation, config)

    # 0.1.34 — memory hygiene hint. When either USER.md or MEMORY.md
    # is bloated (near file cap, or any section overflowing), surface
    # a one-line nudge. Same shape as the proxy preflight: detect,
    # show the remedy, don't block.
    try:
        from anthill.core.memory_files import (
            read_nation_memory,
            read_user_md,
        )
        from anthill.core.memory_hygiene import needs_hygiene

        user_bloated = needs_hygiene(read_user_md(config.home))
        nation_bloated = needs_hygiene(
            read_nation_memory(nation_dir(config.home, nation.name))
        )
        if user_bloated or nation_bloated:
            which = []
            if user_bloated:
                which.append("[cyan]USER.md[/cyan]")
            if nation_bloated:
                which.append("[cyan]MEMORY.md[/cyan]")
            console.print(
                f"  [dim]🧹 memory looks bloated ({' + '.join(which)}). "
                f"Run [cyan]/profile consolidate[/cyan] / "
                f"[cyan]/memory consolidate[/cyan] to dedup + archive.[/dim]"
            )
    except Exception:  # noqa: BLE001 — preflight must never block
        pass

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
        # 0.1.37 — background → REPL delivery. Before every prompt,
        # surface any background jobs (started from THIS session via
        # `/bg ask` slash or by the user explicitly tagging origin)
        # that have completed since the last check. Mirrors Hermes's
        # "Results arrive in the same chat automatically when the
        # task finishes." Best-effort: file-system errors swallowed
        # so the prompt never gets blocked by stale bg state.
        _surface_pending_bg_deliveries(nation, config, stats)

        try:
            line = _read_request_line(prompt="» ")
        except (KeyboardInterrupt, EOFError):
            console.print()
            console.print("[dim]bye.[/dim]")
            # 0.1.35 — graceful close marker on the session file so
            # the picker can distinguish clean exits from crashes
            # later if needed. Best-effort; missing file is fine.
            try:
                from anthill.core.sessions import end_session
                if getattr(stats, "session", None) is not None:
                    end_session(stats.session, reason="user_quit")
            except Exception:  # noqa: BLE001
                pass
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
            elif cmd == "timing" or cmd == "timings":
                # 0.1.52 — per-task_type + per-phase latency aggregates
                # from the current session JSONL. Lets the user see
                # where seconds go ("research subtasks have median 18s,
                # scout 1.5s") without grepping logs.
                _show_timing(config, nation, stats)
            elif cmd == "clear":
                console.clear()
                # 0.1.28 — also reset the rolling conversation window
                # so the next ask doesn't get prepended with whatever
                # was on screen before /clear.
                stats.conversation.reset()
                _print_status_bar(nation, stats)
            elif cmd == "model":
                _handle_model_cmd(rest)
            elif cmd == "nation":
                # Switching nations breaks the conversational thread;
                # different nation = different organism = different
                # memory. Drop the window.
                stats.conversation.reset()
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
                # 0.1.56 — `/setup browser` is a separate one-shot path
                # that doesn't touch model config — installs Playwright +
                # chromium so the 0.1.54 URL-fetch fallback can actually
                # fire. Idempotent; safe to re-run.
                arg = rest.strip().lower()
                if arg in ("browser", "playwright", "chromium"):
                    from anthill.core.browser_setup import ensure_browser
                    console.print(
                        "[bold]Setting up browser fallback for URL fetching[/bold]"
                    )
                    result = ensure_browser(on_progress=console.print)
                    if result.ok:
                        if result.steps_taken:
                            console.print(
                                f"  [green]✓[/green] done. "
                                f"[dim]ran: {', '.join(result.steps_taken)}[/dim]"
                            )
                            console.print(
                                "  [dim]next URL ask uses Playwright when "
                                "httpx hits a login wall or thin content."
                                "[/dim]"
                            )
                        # else: already-ready message printed by ensure_browser
                    else:
                        console.print(
                            f"  [red]✗ setup failed:[/red] {result.error}"
                        )
                else:
                    from anthill.cli.setup_cmd import run_wizard
                    run_wizard(force=False)
                    refreshed = load_nation(nation.name, config.home)
                    if refreshed is not None:
                        nation = refreshed
            elif cmd in ("bg", "background"):
                # 0.1.37 — `/bg <ask>` fires a background ask AND tags
                # its origin as this REPL session, so the
                # delivery-notifier above picks it up next prompt.
                # `/bg list` shows recent jobs; `/bg show <id>` views.
                arg = rest.strip()
                sub = arg.split(" ", 1)[0].lower() if arg else ""
                tail = arg.split(" ", 1)[1].strip() if " " in arg else ""
                ndir = nation_dir(config.home, nation.name)
                if sub == "list":
                    from anthill.core.background import list_jobs
                    jobs = list_jobs(ndir)
                    if not jobs:
                        console.print("  [dim]No background jobs yet.[/dim]")
                    else:
                        for j in jobs[:10]:
                            icon = {
                                "running": "•",
                                "completed": "✓",
                                "failed": "✗",
                                "died": "?",
                                "cancelled": "·",
                            }.get(j.status, "·")
                            req = j.request.replace("\n", " ")[:60]
                            console.print(
                                f"    {icon} [cyan]{j.job_id[:8]}[/cyan] "
                                f"[dim]{j.status} · {j.runtime_seconds:.0f}s[/dim]  "
                                f"{req}"
                            )
                elif sub == "show":
                    if not tail:
                        console.print(
                            "[yellow]Usage: /bg show <job-id-or-prefix>[/yellow]"
                        )
                    else:
                        from anthill.core.background import load_job, read_log
                        j = load_job(tail, ndir)
                        if j is None:
                            console.print(
                                f"[red]No background job matches '{tail}'.[/red]"
                            )
                        else:
                            console.print(
                                f"  [bold]{j.job_id}[/bold] "
                                f"[dim]{j.status} · {j.runtime_seconds:.0f}s[/dim]"
                            )
                            console.print(f"  [dim]request:[/dim] {j.request}")
                            log = read_log(j)
                            if log.strip():
                                console.print(log[-2000:])
                elif sub in ("", "ask"):
                    request_text = tail or arg
                    if sub == "ask":
                        request_text = tail
                    if not request_text:
                        console.print(
                            "[yellow]Usage: /bg ask <request> | "
                            "/bg list | /bg show <id>[/yellow]"
                        )
                    else:
                        from anthill.core.background import start_background
                        session = getattr(stats, "session", None)
                        ndir.mkdir(parents=True, exist_ok=True)
                        job = start_background(
                            request_text, nation.name, ndir,
                            origin_surface="repl",
                            origin_session_id=(
                                session.session_id if session else ""
                            ),
                        )
                        console.print(
                            f"  [green]Started[/green] background ask "
                            f"[cyan]{job.job_id[:8]}[/cyan] "
                            f"[dim](pid {job.pid})[/dim]"
                        )
                        console.print(
                            "  [dim]It'll surface here when done; "
                            "you can keep working.[/dim]"
                        )
                else:
                    console.print(
                        "[yellow]Usage: /bg ask <request> | "
                        "/bg list | /bg show <id>[/yellow]"
                    )
            elif cmd in ("session", "sessions"):
                # 0.1.35 — inspect / list saved sessions. Mostly a
                # debugging aid; main entry is `anthill --resume`.
                from anthill.core.sessions import list_sessions
                metas = list_sessions(
                    config.home, limit=10, nation_name=nation.name,
                )
                current = getattr(stats, "session", None)
                console.print(
                    f"  [dim]current: [cyan]"
                    f"{current.session_id if current else '(none)'}"
                    f"[/cyan]  "
                    f"({current.turn_count if current else 0} turn(s))"
                    f"[/dim]"
                )
                if not metas:
                    console.print("  [dim]No saved sessions yet.[/dim]")
                else:
                    import time as _time
                    console.print("  [bold]Recent sessions:[/bold]")
                    for m in metas:
                        ago = _time.time() - m.last_turn_at
                        when = (
                            f"{int(ago // 60)}m" if ago < 3600
                            else f"{int(ago // 3600)}h" if ago < 86400
                            else f"{int(ago // 86400)}d"
                        )
                        head = m.first_request.replace("\n", " ")[:60]
                        active = "★" if current and current.session_id == m.session_id else " "
                        console.print(
                            f"    {active} [cyan]{m.session_id[:14]}[/cyan] "
                            f"[dim]{when} ago · {m.turn_count} turn(s)[/dim]  "
                            f"{head}"
                        )
                    console.print(
                        "  [dim]Reopen any with [cyan]anthill --resume <id>[/cyan][/dim]"
                    )
            elif cmd == "recall":
                # 0.1.31 — full-text search over THIS nation's history.
                # Fills the gap between in-session window (0.1.28) and
                # distilled long-term memory (0.1.29). Hermes calls
                # this `session_search`; we wrap the SQLite FTS5
                # query directly.
                query = rest.strip()
                if not query:
                    console.print(
                        "[yellow]Usage: /recall <text to search for>[/yellow]"
                    )
                else:
                    import time as _time
                    from anthill.core.recall import search_history
                    hits = search_history(
                        nation_dir(config.home, nation.name), query, k=5,
                    )
                    if not hits:
                        console.print(
                            f"  [dim]No matches for [cyan]{query}[/cyan].[/dim]"
                        )
                    else:
                        console.print(
                            f"  [bold]{len(hits)} match(es)[/bold] "
                            f"[dim]for [cyan]{query}[/cyan]:[/dim]"
                        )
                        for h in hits:
                            ago = _time.time() - h.timestamp
                            days = max(0, int(ago // 86400))
                            when = (
                                "today" if days == 0
                                else "1 day ago" if days == 1
                                else f"{days} days ago"
                            )
                            req_short = h.request.replace("\n", " ")[:80]
                            if len(h.request) > 80:
                                req_short += "…"
                            console.print(
                                f"  [dim]·[/dim] [cyan]{h.entry_id}[/cyan] "
                                f"[dim]{when}[/dim]  {req_short}"
                            )
                            if h.output_snippet:
                                console.print(
                                    f"    [dim]↳ {h.output_snippet[:120]}…[/dim]"
                                )
            elif cmd in ("memory", "mem"):
                # 0.1.34 — `/memory consolidate` runs the hygiene pass
                # (dedup near-duplicates, archive overflow to
                # MEMORY-ARCHIVE.md). Routed before the original
                # 0.1.29 view/edit handler so the new subcommand
                # takes precedence; falls through otherwise.
                if rest.strip().lower() in ("consolidate", "hygiene", "clean"):
                    from anthill.core.memory_hygiene import (
                        consolidate_nation_memory,
                    )
                    report = consolidate_nation_memory(
                        nation_dir(config.home, nation.name)
                    )
                    if report.changed:
                        console.print(
                            f"  [green]✓[/green] [bold]MEMORY.md[/bold] "
                            f"consolidated: "
                            f"[cyan]{report.deduped}[/cyan] dup(s) removed, "
                            f"[cyan]{report.archived}[/cyan] archived "
                            f"[dim]({report.bytes_before} → "
                            f"{report.bytes_after} bytes)[/dim]"
                        )
                        _load_memory_into_nation(nation, config)
                    else:
                        console.print(
                            "  [dim]MEMORY.md is already clean.[/dim]"
                        )
                    continue
                # Otherwise fall through to the existing handler below.
                # 0.1.29 — view / edit the per-nation MEMORY.md.
                from anthill.core.memory_files import (
                    ensure_nation_memory,
                    nation_memory_path,
                    read_nation_memory,
                )
                ndir = nation_dir(config.home, nation.name)
                action = rest.strip().lower()
                if action in ("edit", "e"):
                    path = ensure_nation_memory(ndir, nation.name)
                    _edit_in_external_editor(path)
                    _load_memory_into_nation(nation, config)
                    console.print("  [green]✓[/green] memory reloaded")
                elif action in ("path", "where"):
                    console.print(f"  [cyan]{nation_memory_path(ndir)}[/cyan]")
                else:
                    text = read_nation_memory(ndir)
                    if not text.strip():
                        console.print(
                            "  [dim]No nation memory yet. "
                            "Try [cyan]/memory edit[/cyan] or "
                            "[cyan]/remember <line>[/cyan].[/dim]"
                        )
                    else:
                        console.print(text)
            elif cmd in ("profile", "preferences", "prefs"):
                # 0.1.29 — view / edit the global USER.md.
                # 0.1.32 — `/profile accept [kind]` / `/profile skip [kind]`
                # commit or drop the inferences surfaced at session start.
                from anthill.core.memory_files import (
                    ensure_user_md,
                    read_user_md,
                    user_md_path,
                )
                action = rest.strip()
                head = action.split()[0].lower() if action else ""
                tail = action.split(maxsplit=1)[1].strip() if " " in action else ""
                if head in ("edit", "e"):
                    path = ensure_user_md(config.home)
                    _edit_in_external_editor(path)
                    _load_memory_into_nation(nation, config)
                    console.print("  [green]✓[/green] profile reloaded")
                elif head in ("path", "where"):
                    console.print(f"  [cyan]{user_md_path(config.home)}[/cyan]")
                elif head in ("accept", "confirm", "yes", "y"):
                    _accept_inferences(nation, config, tail or None)
                elif head in ("skip", "no", "n", "dismiss"):
                    _skip_inferences(tail or None)
                elif head in ("consolidate", "hygiene", "clean"):
                    # 0.1.34 — same shape as /memory consolidate.
                    from anthill.core.memory_hygiene import (
                        consolidate_user_md,
                    )
                    report = consolidate_user_md(config.home)
                    if report.changed:
                        console.print(
                            f"  [green]✓[/green] [bold]USER.md[/bold] "
                            f"consolidated: "
                            f"[cyan]{report.deduped}[/cyan] dup(s) removed, "
                            f"[cyan]{report.archived}[/cyan] archived "
                            f"[dim]({report.bytes_before} → "
                            f"{report.bytes_after} bytes)[/dim]"
                        )
                        _load_memory_into_nation(nation, config)
                    else:
                        console.print("  [dim]USER.md is already clean.[/dim]")
                elif head in ("pending", "noticed"):
                    if not _PENDING_INFERENCES:
                        console.print("  [dim]No pending inferences.[/dim]")
                    else:
                        for inf in _PENDING_INFERENCES:
                            pct = int(inf.confidence * 100)
                            console.print(
                                f"  [cyan]{inf.kind}[/cyan] "
                                f"[dim]({pct}%)[/dim]  {inf.summary}"
                            )
                else:
                    text = read_user_md(config.home)
                    if not text.strip():
                        console.print(
                            "  [dim]No user profile yet. "
                            "Try [cyan]/profile edit[/cyan] or "
                            "[cyan]/remember-me <line>[/cyan].[/dim]"
                        )
                    else:
                        console.print(text)
            elif cmd == "remember":
                # 0.1.29 — append a timestamped line to this nation's
                # MEMORY.md. Default section: "Lessons".
                line = rest.strip()
                if not line:
                    console.print(
                        "[yellow]Usage: /remember <line of context to keep>[/yellow]"
                    )
                else:
                    from anthill.core.memory_files import append_nation_memory
                    ok = append_nation_memory(
                        nation_dir(config.home, nation.name),
                        line,
                        nation_name=nation.name,
                    )
                    if ok:
                        _load_memory_into_nation(nation, config)
                        console.print(
                            "  [green]✓[/green] noted under "
                            "[cyan]MEMORY.md / Lessons[/cyan]"
                        )
            elif cmd in ("remember-me", "rememberme"):
                # 0.1.29 — append a timestamped line to USER.md.
                # Default section: "Preferences".
                line = rest.strip()
                if not line:
                    console.print(
                        "[yellow]Usage: /remember-me <line about yourself>[/yellow]"
                    )
                else:
                    from anthill.core.memory_files import append_user_md
                    ok = append_user_md(config.home, line)
                    if ok:
                        _load_memory_into_nation(nation, config)
                        console.print(
                            "  [green]✓[/green] noted under "
                            "[cyan]USER.md / Preferences[/cyan]"
                        )
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
            elif cmd == "skill":
                # 0.1.42 — `/skill save <name>` distills the most-recent
                # complex ask into a saved recipe. The recipe is keyed
                # off the last_ask record so it picks up the actual
                # plan + request shape Anthill just ran.
                arg = rest.strip()
                sub = arg.split(" ", 1)[0].lower() if arg else ""
                tail = arg.split(" ", 1)[1].strip() if " " in arg else ""
                ndir = nation_dir(config.home, nation.name)
                if sub == "save":
                    if not tail:
                        console.print(
                            "[yellow]Usage: /skill save <skill-name>[/yellow]"
                        )
                    else:
                        try:
                            from anthill.core.feedback import load_last_ask
                            from anthill.core.recipes import (
                                Recipe,
                                RecipeSubtask,
                                save_recipe,
                            )
                            from anthill.core.skill_match import suggest_distillation
                            last = load_last_ask(ndir)
                            if last is None:
                                console.print(
                                    "  [yellow]No recent ask to distill yet.[/yellow]"
                                )
                            else:
                                # Pull the plan from the last ask. last_ask
                                # stores task_types; pair them with a
                                # generic template like the original request.
                                sug = suggest_distillation(
                                    last.request,
                                    [tt for _, tt in last.pairs],
                                )
                                recipe = Recipe(
                                    name=tail,
                                    template=sug.template_seed,
                                    description=last.request[:120],
                                    subtasks=[
                                        RecipeSubtask(
                                            task_type=tt,
                                            prompt_template=sug.template_seed,
                                            depends_on=[],
                                        )
                                        for _, tt in last.pairs
                                    ],
                                )
                                save_recipe(recipe, ndir)
                                console.print(
                                    f"  [green]✓[/green] saved skill "
                                    f"[cyan]{tail}[/cyan] "
                                    f"[dim]({len(recipe.subtasks)} subtask(s)) — "
                                    f"will auto-match similar future asks[/dim]"
                                )
                        except Exception as e:  # noqa: BLE001
                            console.print(f"  [red]save failed: {e}[/red]")
                elif sub == "list" or sub == "":
                    try:
                        from anthill.core.recipes import list_recipes
                        from anthill.core.skill_stats import (
                            format_skill_stats,
                            sort_recipes_by_usage,
                        )
                        recipes = list_recipes(ndir)
                        if not recipes:
                            console.print(
                                "  [dim]No saved skills yet. They auto-save "
                                "after a complex ask succeeds with refusal-retry, "
                                "or run [cyan]/skill save <name>[/cyan] manually."
                                "[/dim]"
                            )
                        else:
                            # 0.1.50 — order by usage so the workhorses
                            # surface first, and unused skills sink to
                            # the bottom where they're easy to spot.
                            ordered = sort_recipes_by_usage(recipes)
                            used = sum(1 for r in ordered if r.run_count > 0)
                            console.print(
                                f"  [bold]{len(recipes)} saved skill(s)[/bold] "
                                f"[dim]({used} actually used)[/dim]"
                            )
                            for r in ordered:
                                desc = (
                                    r.description[:60] + "…"
                                    if len(r.description) > 60
                                    else r.description
                                )
                                stats = format_skill_stats(r)
                                console.print(
                                    f"    [cyan]{r.name}[/cyan] "
                                    f"[dim]({len(r.subtasks)} subtask(s)) "
                                    f"{stats} {desc}[/dim]"
                                )
                    except Exception as e:  # noqa: BLE001
                        console.print(f"  [red]{e}[/red]")
                elif sub == "rm" or sub == "remove":
                    # 0.1.51 — direct removal by name. Surgical
                    # delete; for batch cleanup of stale skills,
                    # /skill prune is friendlier.
                    if not tail:
                        console.print(
                            "[yellow]Usage: /skill rm <skill-name>[/yellow]"
                        )
                    else:
                        try:
                            from anthill.core.recipes import remove_recipe
                            ok = remove_recipe(tail, ndir)
                            if ok:
                                console.print(
                                    f"  [green]✓[/green] removed skill "
                                    f"[cyan]{tail}[/cyan]"
                                )
                            else:
                                console.print(
                                    f"  [yellow]no such skill: {tail}[/yellow]"
                                )
                        except Exception as e:  # noqa: BLE001
                            console.print(f"  [red]rm failed: {e}[/red]")
                elif sub == "prune":
                    # 0.1.51 — bulk cleanup of skills that have been
                    # on disk > 14 days with 0 matches. The 0.1.50
                    # /skill list already flags these as 🌫 stale;
                    # this command removes them in one shot.
                    try:
                        from anthill.core.recipes import (
                            list_recipes,
                            remove_recipe,
                        )
                        from anthill.core.skill_stats import partition_stale
                        recipes = list_recipes(ndir)
                        stale, _ = partition_stale(recipes)
                        if not stale:
                            console.print(
                                "  [dim]nothing to prune — all saved skills "
                                "have been used recently or are still fresh."
                                "[/dim]"
                            )
                        else:
                            # No interactive y/n: the user already saw the
                            # 🌫 stale flag in /skill list. Echo each name
                            # as we remove so they can scroll-back to
                            # verify if needed.
                            console.print(
                                f"  [bold]pruning {len(stale)} stale "
                                f"skill(s)[/bold] [dim](>14d, 0 matches)[/dim]"
                            )
                            for r in stale:
                                if remove_recipe(r.name, ndir):
                                    console.print(
                                        f"    [red]✗[/red] {r.name}"
                                    )
                                else:
                                    console.print(
                                        f"    [yellow]?[/yellow] {r.name} "
                                        f"(failed to remove)"
                                    )
                    except Exception as e:  # noqa: BLE001
                        console.print(f"  [red]prune failed: {e}[/red]")
                else:
                    console.print(
                        "[yellow]Usage: /skill save <name> | "
                        "/skill list | /skill rm <name> | /skill prune"
                        "[/yellow]"
                    )
            elif cmd in ("skills",):
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
                # 0.1.33 — `/project on/off/auto` toggles whether
                # the block gets injected. Was implicit-always-on;
                # a real user reported confabulation when running
                # from inside anthill-agent's repo and asking
                # general questions.
                from anthill.core.project import (
                    find_project_root,
                    project_context_block,
                )
                arg = rest.strip().lower()
                if arg in ("on", "off", "auto"):
                    stats.project_inject_mode = arg
                    nation.project_inject_mode = arg
                    state_color = {
                        "on": "green", "off": "dim", "auto": "cyan",
                    }[arg]
                    console.print(
                        f"  project context injection: "
                        f"[{state_color}]{arg}[/{state_color}]"
                    )
                else:
                    _proj = find_project_root()
                    mode = stats.project_inject_mode
                    console.print(
                        f"  [dim]injection: {mode} "
                        f"(toggle with /project on|off|auto)[/dim]"
                    )
                    if _proj is None:
                        console.print(
                            "  [dim]No project detected at cwd or any parent.[/dim]"
                        )
                    else:
                        console.print(
                            f"  [dim]{project_context_block(_proj)}[/dim]"
                        )
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

        # Ask path. Ctrl+C during an ask used to just print
        # "(cancelled)" and return to the prompt. 0.1.36 turns it
        # into a steerable interrupt: cancel cleanly OR redirect
        # the agent with a new instruction without retyping the
        # whole question. Mirrors Hermes "send any message while
        # the agent is working to interrupt it" and Claude Code's
        # "type your correction and press Enter."
        stats.increment_ask()
        current_request = line
        while True:
            try:
                asyncio.run(_handle_ask(current_request, nation, config, stats))
                break  # ask finished normally → exit redirect loop
            except KeyboardInterrupt:
                redirect = _prompt_steer_choice(current_request)
                if redirect is None:
                    # Plain cancel.
                    console.print("  [dim]cancelled[/dim]")
                    break
                # Fire the redirected ask: frame it so the model
                # knows the previous attempt was interrupted and
                # this is the user's correction.
                current_request = (
                    f"[The previous attempt at: {current_request!r} was "
                    f"interrupted by the user. Their correction:]\n"
                    f"{redirect}"
                )
                stats.increment_ask()
                # Loop back into asyncio.run with the new request.
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]Error: {e}[/red]")
                break

        # Reload nation after each ask so persisted state stays in sync.
        refreshed = load_nation(nation.name, config.home)
        if refreshed is not None:
            nation = refreshed

        # Status bar refresh between turns.
        _print_status_bar(nation, stats)

    return 0
