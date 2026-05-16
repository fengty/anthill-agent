"""anthill setup — the first-run wizard.

Three steps:
    1. Add a model. Pick a provider, paste a key, optionally test it.
    2. Found a nation. Pick a name and citizen count.
    3. Optionally add an IM channel (skipped by default).

The whole thing is interruptible — Ctrl+C exits cleanly without
half-writing config. Each step writes its own commit, so even a
partial setup leaves a usable state.

We try to be useful when stdin is not a tty (e.g. piped, CI). In that
case, the wizard refuses with a clear hint instead of hanging.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from rich.console import Console

from anthill.cli.providers_meta import PROVIDER_PRESETS
from anthill.config import AnthillConfig
from anthill.core.nation import Nation
from anthill.core.persistence import save_nation
from anthill.core.router import RouterConfig
from anthill.core.userconfig import (
    ModelEntry,
    UserConfig,
    load_config,
    save_config,
    upsert_secret,
)


console = Console()


@dataclass
class WizardResult:
    model_added: str | None = None
    nation_created: str | None = None
    channels_added: list[str] | None = None


def _is_tty() -> bool:
    return sys.stdin.isatty()


def _prompt(question: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"  {question}{suffix}: ").strip()
        except EOFError:
            raise KeyboardInterrupt
        if answer:
            return answer
        if default is not None:
            return default
        console.print("  [yellow](required)[/yellow]")


def _prompt_secret(question: str) -> str:
    """Like _prompt, but echoes nothing while typing if a real TTY."""
    import getpass
    try:
        return getpass.getpass(f"  {question}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt


def _prompt_yes_no(question: str, *, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        try:
            answer = input(f"  {question} {suffix}: ").strip().lower()
        except EOFError:
            raise KeyboardInterrupt
        if not answer:
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        console.print("  [yellow]please answer y or n[/yellow]")


def _pick_provider() -> str:
    console.print("  Provider:")
    options = list(PROVIDER_PRESETS.items())
    for i, (key, meta) in enumerate(options, start=1):
        console.print(f"    {i}) [cyan]{key}[/cyan]  [dim]{meta.description}[/dim]")
    while True:
        try:
            answer = input("  Choice [1]: ").strip()
        except EOFError:
            raise KeyboardInterrupt
        if not answer:
            return options[0][0]
        # Accept either number or name.
        if answer.isdigit():
            idx = int(answer)
            if 1 <= idx <= len(options):
                return options[idx - 1][0]
        if answer in {k for k, _ in options}:
            return answer
        console.print("  [yellow]not a valid choice[/yellow]")


def _add_model_interactive(user_config: UserConfig) -> tuple[str, str]:
    """Returns (model_name, secret_ref) after writing to disk."""
    provider = _pick_provider()
    preset = PROVIDER_PRESETS[provider]

    default_name = provider if not user_config.find_model(provider) else f"{provider}-2"
    name = _prompt("Save as", default=default_name)

    if preset.needs_base_url:
        base_url = _prompt("Base URL (OpenAI-compatible endpoint)")
    else:
        base_url = None

    model_id = _prompt("Model id", default=preset.default_model)

    api_key = _prompt_secret(preset.key_prompt or "API key")
    if not api_key:
        raise RuntimeError("empty key, aborting")

    secret_ref = f"model.{name}"
    upsert_secret(secret_ref, api_key)

    user_config.models.append(
        ModelEntry(
            name=name,
            provider=provider,
            model=model_id,
            secret_ref=secret_ref,
            base_url=base_url,
        )
    )
    if user_config.default_model is None:
        user_config.default_model = name
    save_config(user_config)
    return name, secret_ref


def _found_nation_interactive(anthill_config: AnthillConfig, default_model: str) -> str:
    """Create the first nation, return its name."""
    nation_name = _prompt("Nation name", default="default")
    try:
        citizens = int(_prompt("Citizens to spawn", default="3"))
    except ValueError:
        citizens = 3

    nation = Nation(
        name=nation_name,
        router_config=RouterConfig(exploration=anthill_config.exploration_rate),
        scout_model=default_model,
    )
    nation.spawn(count=citizens, model=default_model)
    save_nation(nation, anthill_config.home)
    return nation_name


def run_wizard(*, force: bool = False) -> int:
    """Run the wizard. Returns 0 on success, non-zero on user abort/error.

    If force is False and the user already has at least one model
    configured, we ask for confirmation before re-running (so a stray
    `anthill setup` does not surprise the user)."""
    if not _is_tty():
        console.print(
            "[red]anthill setup needs an interactive terminal.[/red]\n"
            "Use [cyan]anthill model add[/cyan] in scripted environments."
        )
        return 2

    user_config = load_config()
    anthill_config = AnthillConfig.load()
    anthill_config.ensure_home()

    if user_config.models and not force:
        console.print(
            f"[yellow]You already have {len(user_config.models)} model(s) "
            f"configured. Re-running setup adds another.[/yellow]"
        )
        if not _prompt_yes_no("Continue?", default_yes=False):
            return 0

    console.print()
    console.print("[bold]Anthill setup[/bold]")
    console.print(
        "[dim]Ctrl+C any time to bail. You can re-run "
        "'anthill setup' or use 'anthill model add' later.[/dim]"
    )
    console.print()

    try:
        # Step 1 — model.
        console.print("[bold cyan][1/3][/bold cyan] Add a model.")
        model_name, _secret_ref = _add_model_interactive(user_config)
        console.print(f"  [green]✓[/green] Saved model '{model_name}'.")
        console.print()

        # Step 2 — nation.
        console.print("[bold cyan][2/3][/bold cyan] Found your first nation.")
        nation_name = _found_nation_interactive(anthill_config, model_name)
        console.print(f"  [green]✓[/green] Founded nation '{nation_name}'.")
        console.print()

        # Step 3 — channels (skipped by default; full UX in v0.2.8).
        console.print("[bold cyan][3/3][/bold cyan] Add an IM channel (optional).")
        console.print(
            "  [dim]Channels can be added later with [cyan]anthill channel add[/cyan].[/dim]"
        )
        console.print()

    except KeyboardInterrupt:
        console.print()
        console.print("[yellow]Setup cancelled. Partial state is on disk.[/yellow]")
        return 130

    console.print("[bold green]Done.[/bold green]")
    console.print()
    console.print("Next:")
    console.print('  [cyan]anthill ask "Hello world"[/cyan]')
    console.print("  [cyan]anthill[/cyan]                 (interactive REPL)")
    return 0
