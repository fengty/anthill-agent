"""0.1.67 — `anthill cron` CLI surface.

Subcommands:
  anthill cron add <schedule> <request> [--channel X] [--target Y]
                                        [--toolset T1 --toolset T2]
                                        [--nation N]
  anthill cron list
  anthill cron rm <id>
  anthill cron tick     # runs all due jobs, marks last_run_at

The `tick` command is the daemon-less entry point: users wire it into
system cron (or launchd / systemd timer) to fire periodically. anthill
itself stays stateless between ticks.
"""

from __future__ import annotations

import asyncio
import time
from typing import Iterable

import click
from rich.console import Console

from anthill.config import AnthillConfig
from anthill.core.cron import (
    JobSpec,
    add_job,
    due_jobs,
    load_jobs,
    remove_job,
    save_jobs,
    validate_schedule,
)


console = Console()


@click.group()
def cron() -> None:
    """Scheduled asks: run a request on @hourly / @daily HH:MM / @every N{s,m,h,d}."""


@cron.command("add")
@click.argument("schedule")
@click.argument("request_text")
@click.option("--nation", default="default", help="Nation to run the ask under")
@click.option("--channel", "channel_name", default=None, help="Channel name to deliver output to")
@click.option("--target", "channel_target", default=None, help="Channel recipient (chat_id / oc_xxx / email)")
@click.option(
    "--toolset",
    "toolset_allow",
    multiple=True,
    help="Plugin name allowed for this job's subtasks (repeatable). Empty = no restriction.",
)
def cron_add(
    schedule: str,
    request_text: str,
    nation: str,
    channel_name: str | None,
    channel_target: str | None,
    toolset_allow: tuple[str, ...],
) -> None:
    """Add a scheduled ask.

    Examples:

      anthill cron add "@daily 09:00" "summarize last week's standups" \\
        --channel slack --target C12345

      anthill cron add "@every 30m" "check for new github issues"

      anthill cron add "@hourly" "tail the production log for errors"
    """
    err = validate_schedule(schedule)
    if err:
        console.print(f"[red]{err}[/red]")
        raise SystemExit(1)

    config = AnthillConfig.load()
    config.ensure_home()
    job = JobSpec(
        schedule=schedule,
        request=request_text,
        nation=nation,
        channel_name=channel_name,
        channel_target=channel_target,
        toolset_allow=list(toolset_allow),
    )
    add_job(config.home, job)
    console.print(
        f"[green]✓[/green] added cron job [cyan]{job.id}[/cyan]: "
        f"[dim]{schedule}[/dim] · {request_text[:60]}"
    )
    if channel_name:
        console.print(
            f"  [dim]→ delivers to channel [cyan]{channel_name}[/cyan] "
            f"({channel_target})[/dim]"
        )


@cron.command("list")
def cron_list() -> None:
    """Show all scheduled jobs."""
    config = AnthillConfig.load()
    jobs = load_jobs(config.home)
    if not jobs:
        console.print(
            "[dim]No cron jobs. Add one with [cyan]anthill cron add[/cyan].[/dim]"
        )
        return
    console.print(f"[bold]{len(jobs)} cron job(s):[/bold]")
    for j in jobs:
        status = "" if j.enabled else " [yellow](disabled)[/yellow]"
        delivery = (
            f" → [cyan]{j.channel_name}[/cyan]"
            if j.channel_name
            else ""
        )
        toolset_tag = (
            f" [dim]toolsets={','.join(j.toolset_allow)}[/dim]"
            if j.toolset_allow
            else ""
        )
        last = (
            _humanize_ago(time.time() - j.last_run_at)
            if j.last_run_at is not None
            else "never run"
        )
        console.print(
            f"  [cyan]{j.id}[/cyan]  [dim]{j.schedule}[/dim]  "
            f"{j.request[:60]}{delivery}{toolset_tag}{status}  "
            f"[dim]({last})[/dim]"
        )


@cron.command("rm")
@click.argument("job_id")
def cron_rm(job_id: str) -> None:
    """Remove a scheduled job by ID or unique prefix."""
    config = AnthillConfig.load()
    if remove_job(config.home, job_id):
        console.print(f"[green]✓[/green] removed cron job [cyan]{job_id}[/cyan]")
    else:
        console.print(
            f"[yellow]no unique match for {job_id!r}[/yellow]"
        )
        raise SystemExit(1)


@cron.command("tick")
@click.option("--dry-run", is_flag=True, help="Show what would run; don't actually run.")
def cron_tick(dry_run: bool) -> None:
    """Run all due jobs once. Intended for `* * * * *  anthill cron tick`
    in system crontab — anthill itself doesn't run a daemon."""
    config = AnthillConfig.load()
    jobs = load_jobs(config.home)
    due = due_jobs(jobs)
    if not due:
        console.print("[dim]No jobs due.[/dim]")
        return
    console.print(f"[bold]{len(due)} job(s) due.[/bold]")
    if dry_run:
        for j in due:
            console.print(
                f"  [yellow]would run[/yellow] [cyan]{j.id}[/cyan] "
                f"{j.request[:60]}"
            )
        return
    asyncio.run(_run_due_jobs(due, jobs, config))


async def _run_due_jobs(
    due: Iterable[JobSpec],
    all_jobs: list[JobSpec],
    config: AnthillConfig,
) -> None:
    """Execute each due job: run the ask, optionally deliver output,
    update last_run_at, save jobs back."""
    from anthill.channels.daemon import _load_or_create_nation
    from anthill.core.persistence import nation_dir
    from anthill.core.userconfig import load_config as _load_user_cfg

    user_cfg = _load_user_cfg()
    for job in due:
        console.print(
            f"[bold]→ running[/bold] [cyan]{job.id}[/cyan]: "
            f"{job.request[:80]}"
        )
        nation = _load_or_create_nation(config, job.nation)
        try:
            result = await nation.ask(
                job.request,
                nation_dir=nation_dir(config.home, nation.name),
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"  [red]✗ {e}[/red]")
            continue
        final = result.final_output or "(no output)"
        console.print(f"  [green]✓[/green] {final[:200]}")

        # Optional channel delivery.
        if job.channel_name and job.channel_target:
            try:
                from anthill.cli.channel_cmd import build_channel
                # Find the entry matching the configured channel name.
                entry = user_cfg.find_channel(job.channel_name)
                if entry is None:
                    console.print(
                        f"  [yellow]channel {job.channel_name!r} not "
                        f"configured — skipping delivery[/yellow]"
                    )
                else:
                    channel_obj = build_channel(entry)
                    if channel_obj is not None:
                        await channel_obj.send(
                            to=job.channel_target, text=final
                        )
                        console.print(
                            f"  [dim]→ delivered to "
                            f"{job.channel_name}[/dim]"
                        )
            except Exception as e:  # noqa: BLE001
                console.print(f"  [yellow]delivery failed: {e}[/yellow]")

        job.last_run_at = time.time()
    save_jobs(all_jobs, config.home)


def _humanize_ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"
