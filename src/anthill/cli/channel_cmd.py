"""anthill channel <subcommand> — manage IM channels from the CLI.

  anthill channel                     list (alias)
  anthill channel list                show every configured channel
  anthill channel add                 interactive: pick kind + paste secrets
  anthill channel add NAME --kind lark --app-id ... --app-secret ...
  anthill channel show NAME           full details, secrets masked
  anthill channel remove NAME         remove + drop secret refs
  anthill channel test NAME           call .ping() against the platform
"""

from __future__ import annotations

import asyncio
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from anthill.core.userconfig import (
    ChannelEntry,
    load_config,
    load_secrets,
    mask,
    remove_secret,
    save_config,
    upsert_secret,
)


console = Console()


# Per-channel "shape" of secrets we need to collect.
CHANNEL_SPECS: dict[str, list[dict[str, Any]]] = {
    "lark": [
        {"key": "app_id", "prompt": "App ID (cli_...)", "kind": "field"},
        {"key": "app_secret", "prompt": "App secret", "kind": "secret"},
        {
            "key": "verification_token",
            "prompt": "Verification token (optional, blank to skip)",
            "kind": "field",
            "optional": True,
        },
    ],
    "telegram": [
        {"key": "bot_token", "prompt": "Bot token (123:abc...)", "kind": "secret"},
    ],
    "slack": [
        {"key": "bot_token", "prompt": "Bot token (xoxb-...)", "kind": "secret"},
    ],
    "wecom": [
        {"key": "corp_id", "prompt": "Corp ID", "kind": "field"},
        {"key": "corp_secret", "prompt": "Corp secret", "kind": "secret"},
        {"key": "agent_id", "prompt": "Agent ID (numeric)", "kind": "field"},
    ],
    # 0.1.60 — Discord bot. Token from dev portal Bot tab; just the
    # token string (no "Bot " prefix — the channel adds that itself).
    "discord": [
        {"key": "bot_token", "prompt": "Bot token (from Discord dev portal)", "kind": "secret"},
    ],
    # 0.1.61 — Email send (SMTP) + 0.1.66 — IMAP receive (optional).
    # smtp_port defaults to 587 (STARTTLS); use 465 for implicit SSL.
    # imap_host blank = send-only channel.
    "email": [
        {"key": "smtp_host", "prompt": "SMTP host (e.g. smtp.gmail.com)", "kind": "field"},
        {"key": "smtp_port", "prompt": "SMTP port (587 STARTTLS / 465 SSL)", "kind": "field"},
        {"key": "username", "prompt": "SMTP username (usually your email)", "kind": "field"},
        {"key": "password", "prompt": "SMTP password / app password", "kind": "secret"},
        {"key": "from_addr", "prompt": "From: address (blank = same as username)", "kind": "field", "optional": True},
        {"key": "imap_host", "prompt": "IMAP host for receive (blank = send-only)", "kind": "field", "optional": True},
        {"key": "imap_port", "prompt": "IMAP port (993 = SSL default)", "kind": "field", "optional": True},
        {"key": "imap_folder", "prompt": "IMAP folder (default INBOX)", "kind": "field", "optional": True},
    ],
}


def _prompt(question: str, *, default: str | None = None, allow_blank: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"  {question}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        if answer:
            return answer
        if default is not None:
            return default
        if allow_blank:
            return ""
        console.print("  [yellow](required)[/yellow]")


def _prompt_secret(question: str) -> str:
    import getpass
    try:
        return getpass.getpass(f"  {question}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt


def _pick_kind() -> str:
    kinds = sorted(CHANNEL_SPECS)
    console.print("  Channel kind:")
    for i, k in enumerate(kinds, start=1):
        console.print(f"    {i}) [cyan]{k}[/cyan]")
    while True:
        try:
            answer = input("  Choice [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        if not answer:
            return kinds[0]
        if answer.isdigit() and 1 <= int(answer) <= len(kinds):
            return kinds[int(answer) - 1]
        if answer in kinds:
            return answer
        console.print("  [yellow]not a valid choice[/yellow]")


def _collect_fields(kind: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Returns (non-secret extras, secret-keyed dict to store)."""
    extras: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    for spec in CHANNEL_SPECS[kind]:
        is_secret = spec["kind"] == "secret"
        is_optional = spec.get("optional", False)
        if is_secret:
            value = _prompt_secret(spec["prompt"])
            if value:
                secrets[spec["key"]] = value
            elif not is_optional:
                raise RuntimeError(f"{spec['key']} is required")
        else:
            value = _prompt(spec["prompt"], allow_blank=is_optional)
            if value:
                extras[spec["key"]] = value
    return extras, secrets


@click.group(invoke_without_command=True)
@click.pass_context
def channel(ctx: click.Context) -> None:
    """Manage IM channels."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(channel_list)


@channel.command("list")
def channel_list() -> None:
    """List every configured channel."""
    cfg = load_config()
    if not cfg.channels:
        console.print(
            "[dim]No channels yet.[/dim] Add one with [cyan]anthill channel add[/cyan]."
        )
        return
    table = Table(title="Configured channels")
    table.add_column("Name", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Extras", style="dim")
    for c in cfg.channels:
        extras = ", ".join(f"{k}={v}" for k, v in c.extra.items() if k != "agent_id") or "—"
        table.add_row(c.name, c.kind, extras)
    console.print(table)


@channel.command("add")
@click.argument("name", required=False)
@click.option("--kind", help="lark / telegram / slack / wecom / discord / email")
@click.option("--app-id", help="Lark App ID")
@click.option("--app-secret", help="Lark App secret")
@click.option("--bot-token", help="Telegram / Slack / Discord bot token")
@click.option("--corp-id", help="WeCom corp id")
@click.option("--corp-secret", help="WeCom corp secret")
@click.option("--agent-id", help="WeCom agent id")
@click.option("--smtp-host", help="Email: SMTP hostname")
@click.option("--smtp-port", help="Email: SMTP port (587 STARTTLS / 465 SSL)")
@click.option("--username", help="Email: SMTP username")
@click.option("--password", help="Email: SMTP password / app password")
@click.option("--from-addr", help="Email: From address (defaults to username)")
@click.option("--imap-host", help="Email: IMAP host for receive (e.g. imap.gmail.com)")
@click.option("--imap-port", help="Email: IMAP port (993 = SSL default)")
@click.option("--imap-folder", help="Email: IMAP folder to poll (default INBOX)")
def channel_add(
    name: str | None,
    kind: str | None,
    app_id: str | None,
    app_secret: str | None,
    bot_token: str | None,
    corp_id: str | None,
    corp_secret: str | None,
    agent_id: str | None,
    smtp_host: str | None,
    smtp_port: str | None,
    username: str | None,
    password: str | None,
    from_addr: str | None,
    imap_host: str | None,
    imap_port: str | None,
    imap_folder: str | None,
) -> None:
    """Add a channel. With no flags, runs interactively."""
    cfg = load_config()

    # Interactive path when key fields are missing.
    if not name or not kind:
        from anthill.cli.setup_cmd import _is_tty

        if not _is_tty():
            console.print(
                "[red]Non-interactive: pass --kind and the relevant fields.[/red]"
            )
            raise SystemExit(2)
        if not kind:
            kind = _pick_kind()
        if not name:
            default_name = kind if not cfg.find_channel(kind) else f"{kind}-2"
            name = _prompt("Save as", default=default_name)
        try:
            extras, secrets_map = _collect_fields(kind)
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            raise SystemExit(1) from e
    else:
        # Non-interactive: read everything from flags.
        if kind not in CHANNEL_SPECS:
            console.print(f"[red]Unknown kind '{kind}'.[/red]")
            raise SystemExit(1)
        flag_map = {
            "lark": {"app_id": app_id, "app_secret": app_secret},
            "telegram": {"bot_token": bot_token},
            "slack": {"bot_token": bot_token},
            "wecom": {
                "corp_id": corp_id,
                "corp_secret": corp_secret,
                "agent_id": agent_id,
            },
            "discord": {"bot_token": bot_token},
            "email": {
                "smtp_host": smtp_host,
                "smtp_port": smtp_port or "587",
                "username": username,
                "password": password,
                "from_addr": from_addr,
                "imap_host": imap_host,
                "imap_port": imap_port or "993",
                "imap_folder": imap_folder or "INBOX",
            },
        }
        extras = {}
        secrets_map = {}
        for spec in CHANNEL_SPECS[kind]:
            value = flag_map[kind].get(spec["key"])
            if not value:
                if spec.get("optional"):
                    continue
                console.print(
                    f"[red]Missing --{spec['key'].replace('_', '-')} for kind {kind}.[/red]"
                )
                raise SystemExit(1)
            if spec["kind"] == "secret":
                secrets_map[spec["key"]] = value
            else:
                extras[spec["key"]] = value

    if cfg.find_channel(name):
        console.print(f"[red]Channel '{name}' already exists.[/red]")
        raise SystemExit(1)

    # Secrets stored under namespaced refs of the form 'channel.NAME.FIELD'.
    # We don't persist the ref mapping; it's always reconstructible.
    secret_ref_root = f"channel.{name}"
    for field, value in secrets_map.items():
        upsert_secret(f"{secret_ref_root}.{field}", value)

    cfg.channels.append(
        ChannelEntry(
            name=name,
            kind=kind,
            secret_ref=secret_ref_root,
            extra=extras,
        )
    )
    save_config(cfg)
    console.print(f"[green]✓[/green] Added channel '{name}' (kind={kind}).")


def _secret_fields_for(kind: str) -> list[str]:
    """Names of every secret-typed field this kind expects."""
    return [s["key"] for s in CHANNEL_SPECS.get(kind, []) if s["kind"] == "secret"]


@channel.command("show")
@click.argument("name")
def channel_show(name: str) -> None:
    """Show one channel's full configuration."""
    cfg = load_config()
    entry = cfg.find_channel(name)
    if entry is None:
        console.print(f"[red]No channel named '{name}'.[/red]")
        raise SystemExit(1)
    secrets = load_secrets()
    console.print(f"[bold]{entry.name}[/bold]")
    console.print(f"  kind:        {entry.kind}")
    for k, v in entry.extra.items():
        console.print(f"  {k}: {v}")
    for field in _secret_fields_for(entry.kind):
        ref = f"channel.{name}.{field}"
        value = secrets.get(ref, "")
        console.print(f"  {field}: {mask(value) if value else '[red]missing[/red]'}")


@channel.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True)
def channel_remove(name: str, yes: bool) -> None:
    """Remove a channel and its secrets."""
    cfg = load_config()
    entry = cfg.find_channel(name)
    if entry is None:
        console.print(f"[red]No channel named '{name}'.[/red]")
        raise SystemExit(1)
    if not yes:
        try:
            answer = input(f"Remove channel '{name}'? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if answer not in ("y", "yes"):
            console.print("Cancelled.")
            return
    for field in _secret_fields_for(entry.kind):
        remove_secret(f"channel.{name}.{field}")
    cfg.channels = [c for c in cfg.channels if c.name != name]
    save_config(cfg)
    console.print(f"[green]✓[/green] Removed channel '{name}'.")


@channel.command("test")
@click.argument("name")
def channel_test(name: str) -> None:
    """Call .ping() on the configured channel — validates credentials."""
    cfg = load_config()
    entry = cfg.find_channel(name)
    if entry is None:
        console.print(f"[red]No channel named '{name}'.[/red]")
        raise SystemExit(1)
    channel_obj = build_channel(entry)
    if channel_obj is None:
        console.print(f"[red]Could not build {entry.kind} channel — secrets missing?[/red]")
        raise SystemExit(1)

    console.print(f"Testing '{name}' ({entry.kind})... ", end="")
    ok = asyncio.run(channel_obj.ping())
    if ok:
        console.print("[green]✓ ok[/green]")
    else:
        console.print("[red]✗ failed[/red]")
        raise SystemExit(1)


def build_channel(entry: ChannelEntry):  # noqa: ANN201
    """Build a runtime Channel object from a ChannelEntry.

    Public helper — used by `channel test` here and by the daemon
    (v0.2.8 wiring) when starting up.
    """
    secrets = load_secrets()

    def s(field: str) -> str | None:
        return secrets.get(f"channel.{entry.name}.{field}")

    if entry.kind == "lark":
        from anthill.channels.lark import LarkChannel
        app_id = entry.extra.get("app_id")
        app_secret = s("app_secret")
        if not (app_id and app_secret):
            return None
        return LarkChannel(app_id=app_id, app_secret=app_secret)
    if entry.kind == "telegram":
        from anthill.channels.telegram import TelegramChannel
        token = s("bot_token")
        if not token:
            return None
        return TelegramChannel(bot_token=token)
    if entry.kind == "slack":
        from anthill.channels.slack import SlackChannel
        token = s("bot_token")
        if not token:
            return None
        return SlackChannel(bot_token=token)
    if entry.kind == "wecom":
        from anthill.channels.wecom import WeComChannel
        corp_id = entry.extra.get("corp_id")
        corp_secret = s("corp_secret")
        agent_id_raw = entry.extra.get("agent_id")
        if not (corp_id and corp_secret and agent_id_raw):
            return None
        return WeComChannel(
            corp_id=corp_id,
            corp_secret=corp_secret,
            agent_id=int(agent_id_raw),
        )
    if entry.kind == "discord":
        # 0.1.60 — Discord. Single bot_token from the dev portal Bot
        # tab; remember to copy the literal string (no "Bot " prefix —
        # the channel adds that automatically).
        from anthill.channels.discord import DiscordChannel
        token = s("bot_token")
        if not token:
            return None
        return DiscordChannel(bot_token=token)
    if entry.kind == "email":
        # 0.1.61 — Email (SMTP send). Defaults to port 587 (STARTTLS)
        # when entry.extra doesn't have it. from_addr falls back to
        # username inside EmailChannel.__init__.
        from anthill.channels.email import EmailChannel
        smtp_host = entry.extra.get("smtp_host")
        username = entry.extra.get("username")
        password = s("password")
        if not (smtp_host and username and password):
            return None
        port_raw = entry.extra.get("smtp_port") or "587"
        try:
            smtp_port = int(port_raw)
        except (TypeError, ValueError):
            smtp_port = 587
        # 0.1.66 — IMAP receive is opt-in. None imap_host = send-only.
        imap_host = entry.extra.get("imap_host") or None
        imap_port_raw = entry.extra.get("imap_port") or "993"
        try:
            imap_port = int(imap_port_raw)
        except (TypeError, ValueError):
            imap_port = 993
        imap_folder = entry.extra.get("imap_folder") or "INBOX"
        return EmailChannel(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            username=username,
            password=password,
            from_addr=entry.extra.get("from_addr") or None,
            imap_host=imap_host,
            imap_port=imap_port,
            imap_folder=imap_folder,
        )
    return None
