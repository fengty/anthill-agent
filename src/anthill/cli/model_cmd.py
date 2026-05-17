"""anthill model <subcommand> — CRUD for configured models.

  anthill model               (== list)
  anthill model list
  anthill model add           (interactive)
  anthill model add NAME --provider deepseek --model deepseek-chat --key sk-...
  anthill model use NAME      (set default)
  anthill model show NAME
  anthill model rename OLD NEW
  anthill model remove NAME
  anthill model test NAME     (validate the API key works)
"""

from __future__ import annotations

import asyncio
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from anthill.cli.providers_meta import PROVIDER_PRESETS
from anthill.cli.setup_cmd import _add_model_interactive
from anthill.core.userconfig import (
    ModelEntry,
    load_config,
    load_secrets,
    mask,
    remove_secret,
    save_config,
    upsert_secret,
)


console = Console()


@click.group(invoke_without_command=True)
@click.pass_context
def model(ctx: click.Context) -> None:
    """Manage configured models."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(model_list)


@model.command("list")
def model_list() -> None:
    """List all configured models."""
    cfg = load_config()
    if not cfg.models:
        console.print(
            "[dim]No models configured. Run [cyan]anthill setup[/cyan] or "
            "[cyan]anthill model add[/cyan].[/dim]"
        )
        return
    secrets = load_secrets()
    table = Table(title="Configured models")
    table.add_column("Name", style="cyan")
    table.add_column("Provider", style="magenta")
    table.add_column("Model")
    table.add_column("Key", style="dim")
    table.add_column("Default", style="green", justify="center")
    for m in cfg.models:
        key_display = mask(secrets.get(m.secret_ref, ""))
        default_mark = "★" if m.name == cfg.default_model else ""
        table.add_row(m.name, m.provider, m.model, key_display or "[red]missing[/red]", default_mark)
    console.print(table)


@model.command("add")
@click.argument("name", required=False)
@click.option("--provider", help="Provider name (deepseek, minimax, openai, anthropic, custom).")
@click.option("--model", "model_id", help="Model id (e.g. deepseek-chat).")
@click.option("--key", help="API key. If omitted, you will be prompted.")
@click.option("--base-url", help="Required for provider=custom.")
@click.option("--set-default", is_flag=True, help="Mark this as the default model.")
def model_add(
    name: str | None,
    provider: str | None,
    model_id: str | None,
    key: str | None,
    base_url: str | None,
    set_default: bool,
) -> None:
    """Add a model. With no flags, runs interactively."""
    cfg = load_config()

    # If any flag is missing, fall back to the interactive flow.
    if not (name and provider and model_id and key):
        if any([name, provider, model_id, key, base_url]):
            console.print(
                "[yellow]Partial flags — falling back to interactive prompts for the rest.[/yellow]"
            )
        from anthill.cli.setup_cmd import _is_tty
        if not _is_tty():
            console.print(
                "[red]Non-interactive: pass --provider --model --key (and --base-url for custom) "
                "to add a model without prompts.[/red]"
            )
            raise SystemExit(2)
        # Hand off to wizard's add helper, which handles all prompting.
        model_name, _ = _add_model_interactive(cfg)
        console.print(f"[green]✓[/green] Added '{model_name}'.")
        return

    if cfg.find_model(name):
        console.print(f"[red]Model '{name}' already exists. Use a different name or `model remove`.[/red]")
        raise SystemExit(1)
    if provider not in PROVIDER_PRESETS:
        console.print(
            f"[red]Unknown provider '{provider}'. "
            f"Known: {', '.join(sorted(PROVIDER_PRESETS))}.[/red]"
        )
        raise SystemExit(1)
    if PROVIDER_PRESETS[provider].needs_base_url and not base_url:
        console.print(f"[red]Provider '{provider}' requires --base-url.[/red]")
        raise SystemExit(1)

    secret_ref = f"model.{name}"
    upsert_secret(secret_ref, key)
    cfg.models.append(
        ModelEntry(
            name=name,
            provider=provider,
            model=model_id,
            secret_ref=secret_ref,
            base_url=base_url,
        )
    )
    if set_default or cfg.default_model is None:
        cfg.default_model = name
    save_config(cfg)
    console.print(f"[green]✓[/green] Added '{name}' (provider={provider}, model={model_id}).")


@model.command("use")
@click.argument("name")
def model_use(name: str) -> None:
    """Mark a model as the default."""
    cfg = load_config()
    if cfg.find_model(name) is None:
        console.print(f"[red]No model named '{name}'.[/red]")
        raise SystemExit(1)
    cfg.default_model = name
    save_config(cfg)
    console.print(f"[green]✓[/green] Default model is now '{name}'.")


@model.command("show")
@click.argument("name")
def model_show(name: str) -> None:
    """Show one model's full configuration."""
    cfg = load_config()
    entry = cfg.find_model(name)
    if entry is None:
        console.print(f"[red]No model named '{name}'.[/red]")
        raise SystemExit(1)
    secrets = load_secrets()
    key = secrets.get(entry.secret_ref, "")
    console.print(f"[bold]{entry.name}[/bold]")
    console.print(f"  provider:   {entry.provider}")
    console.print(f"  model:      {entry.model}")
    console.print(f"  base_url:   {entry.base_url or '(default for provider)'}")
    console.print(f"  secret_ref: {entry.secret_ref}")
    console.print(f"  key:        {mask(key) if key else '[red]missing[/red]'}")
    if entry.name == cfg.default_model:
        console.print("  [green]★ default[/green]")


@model.command("rename")
@click.argument("old")
@click.argument("new")
def model_rename(old: str, new: str) -> None:
    """Rename a configured model."""
    cfg = load_config()
    entry = cfg.find_model(old)
    if entry is None:
        console.print(f"[red]No model named '{old}'.[/red]")
        raise SystemExit(1)
    if cfg.find_model(new):
        console.print(f"[red]A model named '{new}' already exists.[/red]")
        raise SystemExit(1)
    # Move the secret too so the ref still resolves.
    secrets = load_secrets()
    if entry.secret_ref in secrets:
        value = secrets[entry.secret_ref]
        new_ref = f"model.{new}"
        upsert_secret(new_ref, value)
        remove_secret(entry.secret_ref)
        entry.secret_ref = new_ref
    entry.name = new
    if cfg.default_model == old:
        cfg.default_model = new
    save_config(cfg)
    console.print(f"[green]✓[/green] Renamed '{old}' to '{new}'.")


@model.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def model_remove(name: str, yes: bool) -> None:
    """Remove a configured model."""
    cfg = load_config()
    entry = cfg.find_model(name)
    if entry is None:
        console.print(f"[red]No model named '{name}'.[/red]")
        raise SystemExit(1)
    if not yes:
        try:
            answer = input(f"Remove '{name}'? This cannot be undone. [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if answer not in ("y", "yes"):
            console.print("Cancelled.")
            return
    cfg.models = [m for m in cfg.models if m.name != name]
    remove_secret(entry.secret_ref)
    if cfg.default_model == name:
        cfg.default_model = cfg.models[0].name if cfg.models else None
    save_config(cfg)
    console.print(f"[green]✓[/green] Removed '{name}'.")


@model.command("test")
@click.argument("name")
def model_test(name: str) -> None:
    """Try a tiny call against the provider to validate the key."""
    cfg = load_config()
    entry = cfg.find_model(name)
    if entry is None:
        console.print(f"[red]No model named '{name}'.[/red]")
        raise SystemExit(1)
    secrets = load_secrets()
    api_key = secrets.get(entry.secret_ref)
    if not api_key:
        console.print(f"[red]No API key found at secret_ref '{entry.secret_ref}'.[/red]")
        raise SystemExit(1)

    console.print(f"Testing '{name}'... ", end="")
    result = asyncio.run(_probe_model(entry, api_key))
    if result["ok"]:
        console.print(
            f"[green]✓ ok[/green] [dim]{result['latency_ms']:.0f}ms, "
            f"{result.get('out_tokens', 0)} tokens[/dim]"
        )
    else:
        console.print(f"[red]✗ {result['error']}[/red]")
        raise SystemExit(1)


@model.group("catalog")
def model_catalog() -> None:
    """Manage the live model catalog (refreshed from providers' /v1/models)."""


@model_catalog.command("refresh")
def model_catalog_refresh() -> None:
    """Pull each configured provider's current model list and cache it locally.

    Uses the API key of the first configured model per provider. Failed
    providers are skipped quietly — the previous cached entry stays. Run
    this whenever providers add or rename models (DeepSeek, OpenAI, etc.
    update frequently and we don't ship a new package for it).
    """
    from anthill.cli.model_catalog import refresh_all
    from anthill.config import AnthillConfig

    home = AnthillConfig.load().home
    home.mkdir(parents=True, exist_ok=True)

    console.print("Refreshing model catalog...")
    catalog = asyncio.run(refresh_all(home))
    if not catalog:
        console.print(
            "[yellow]No providers refreshed. "
            "Configure at least one model with [cyan]anthill model add[/cyan].[/yellow]"
        )
        return

    table = Table(title="Refreshed catalog")
    table.add_column("Provider", style="magenta")
    table.add_column("Models", style="cyan")
    for provider, entry in sorted(catalog.items()):
        sample = ", ".join(entry.models[:5])
        more = f" (+{len(entry.models) - 5} more)" if len(entry.models) > 5 else ""
        table.add_row(provider, f"{sample}{more}")
    console.print(table)


@model_catalog.command("show")
@click.argument("provider", required=False)
def model_catalog_show(provider: str | None) -> None:
    """Show the cached model list, optionally for one provider."""
    from anthill.cli.model_catalog import load_catalog
    from anthill.config import AnthillConfig

    catalog = load_catalog(AnthillConfig.load().home)
    if not catalog:
        console.print(
            "[dim]No live catalog yet. "
            "Run [cyan]anthill model catalog refresh[/cyan] to populate it.[/dim]"
        )
        return
    if provider is not None:
        entry = catalog.get(provider)
        if entry is None:
            console.print(f"[red]No cached entry for '{provider}'.[/red]")
            raise SystemExit(1)
        for m in entry.models:
            console.print(f"  {m}")
        return
    table = Table(title="Live model catalog")
    table.add_column("Provider", style="magenta")
    table.add_column("Model", style="cyan")
    for provider_name, entry in sorted(catalog.items()):
        for m in entry.models:
            table.add_row(provider_name, m)
    console.print(table)


async def _probe_model(entry: ModelEntry, api_key: str) -> dict[str, Any]:
    """Direct httpx test — does not depend on the runtime ModelProvider stack
    yet, so 'anthill model test' works before v0.2.4 wires UserConfig into the
    provider system."""
    import time

    import httpx

    base = entry.base_url or {
        "deepseek": "https://api.deepseek.com/v1",
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "minimax": "https://api.minimax.chat/v1",
    }.get(entry.provider)

    if base is None:
        return {"ok": False, "error": f"unknown provider '{entry.provider}', set base_url"}

    headers: dict[str, str] = {}
    json_payload: dict[str, Any] = {}
    if entry.provider == "anthropic":
        # Anthropic uses x-api-key + anthropic-version
        url = f"{base}/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        json_payload = {
            "model": entry.model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "ok"}],
        }
    else:
        # OpenAI-style
        url = f"{base}/chat/completions"
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        json_payload = {
            "model": entry.model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "ok"}],
        }

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, headers=headers, json=json_payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:200] if e.response else ""
        return {"ok": False, "error": f"HTTP {e.response.status_code} {body}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__}

    latency_ms = (time.perf_counter() - start) * 1000
    out_tokens = 0
    try:
        if entry.provider == "anthropic":
            out_tokens = data.get("usage", {}).get("output_tokens", 0)
        else:
            out_tokens = data.get("usage", {}).get("completion_tokens", 0)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "latency_ms": latency_ms, "out_tokens": out_tokens}
