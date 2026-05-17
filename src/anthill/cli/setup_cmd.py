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


def _prompt_int(question: str, *, default: int, min_val: int = 1, max_val: int = 100) -> int:
    """Ask for an integer; re-prompt on non-int / out-of-range input.

    Returns default only when the user submits an empty line. Anything
    else gets validated explicitly so a typo like '秦' or 'three' shows
    a friendly error instead of silently snapping to the default.
    """
    suffix = f" [{default}]"
    while True:
        try:
            answer = input(f"  {question}{suffix}: ").strip()
        except EOFError:
            raise KeyboardInterrupt
        if not answer:
            return default
        try:
            val = int(answer)
        except ValueError:
            console.print(
                f"  [yellow]'{answer}' is not a whole number. "
                f"Try a number between {min_val} and {max_val}.[/yellow]"
            )
            continue
        if not (min_val <= val <= max_val):
            console.print(
                f"  [yellow]must be between {min_val} and {max_val}[/yellow]"
            )
            continue
        return val


def _pick_model_id(
    *,
    default: str,
    known: tuple[str, ...],
    extra: tuple[str, ...] = (),
) -> str:
    """Pick a model id from a numbered list, with a fallback to custom input.

    ``known`` is the static allow-list from PROVIDER_PRESETS. ``extra`` is
    any additional ids pulled from the live catalog (refreshed via
    ``anthill model catalog refresh``). Both are merged; ``default`` is
    pinned to the top so hitting Enter still works.

    For providers with no known list (e.g. ``custom``), falls back to
    free-text entry — the picker would be empty and useless.
    """
    # Merge while preserving order: default first, then known, then
    # extras the live catalog surfaced.
    seen: set[str] = set()
    options: list[str] = []
    for candidate in (default, *known, *extra):
        if candidate and candidate not in seen:
            seen.add(candidate)
            options.append(candidate)

    if not options or options == [default] and not known:
        # No allow-list — degrade to free-text (the only path for
        # ``custom`` providers where the user knows their own model id).
        try:
            answer = input(f"  Model id [{default}]: ").strip()
        except EOFError:
            raise KeyboardInterrupt
        return answer or default

    console.print("  Model id:")
    for i, name in enumerate(options, start=1):
        marker = "  [dim](default)[/dim]" if name == default else ""
        console.print(f"    {i}) [cyan]{name}[/cyan]{marker}")
    console.print(f"    {len(options) + 1}) [dim]Other (type a custom id)[/dim]")

    while True:
        try:
            answer = input(f"  Choice [1 = {default}]: ").strip()
        except EOFError:
            raise KeyboardInterrupt
        if not answer:
            return default
        if answer.isdigit():
            idx = int(answer)
            if 1 <= idx <= len(options):
                return options[idx - 1]
            if idx == len(options) + 1:
                # User wants to type a custom id — accept anything but
                # still warn if it looks like a known typo (e.g. just
                # "deepseek" instead of "deepseek-chat").
                try:
                    custom = input("  Custom model id: ").strip()
                except EOFError:
                    raise KeyboardInterrupt
                if not custom:
                    console.print("  [yellow](required)[/yellow]")
                    continue
                if custom in options:
                    return custom
                console.print(
                    f"  [yellow]'{custom}' is not in the known list "
                    f"for this provider.[/yellow]"
                )
                try:
                    confirm = input("  Use it anyway? [y/N]: ").strip().lower()
                except EOFError:
                    raise KeyboardInterrupt
                if confirm in ("y", "yes"):
                    return custom
                continue
        # Also accept the model id itself (typed verbatim) — useful for
        # users who paste from docs.
        if answer in options:
            return answer
        console.print("  [yellow]not a valid choice[/yellow]")


# Backwards-compat alias — older tests / external callers expect the
# old name. New code should use ``_pick_model_id``.
_prompt_model_id = _pick_model_id


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

    # Live catalog (if refreshed) augments the static known list.
    from anthill.cli.model_catalog import model_ids_for_provider
    anthill_home = AnthillConfig.load().home
    catalog_ids = model_ids_for_provider(provider, anthill_home)
    # Strip duplicates that are already in known_models so the picker
    # only shows each id once.
    extras = tuple(m for m in catalog_ids if m not in preset.known_models)
    model_id = _pick_model_id(
        default=preset.default_model,
        known=preset.known_models,
        extra=extras,
    )

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
    citizens = _prompt_int("Citizens to spawn", default=3, min_val=1, max_val=50)

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
