"""See multi-model orchestration in action.

The user gives one natural-language request; the Scout decomposes it
into a multi-subtask plan; each subtask is routed to whichever citizen
the pheromone trails favor. This is the headline capability of v0.2:
one ask, many cooperating model calls, with live progress visibility
as each step runs.

What's printed:
1. The plan Scout produced (subtask types + dependencies)
2. Live per-subtask progress as each step starts/finishes
3. Each subtask's output
4. The budget summary if a cap was set

Requires at least one configured model. Set it up once with:

    anthill model add deepseek --provider deepseek \\
      --model deepseek-chat --key sk-... --set-default

Optional second provider so the router has a real choice to make:

    anthill model add minimax --provider minimax \\
      --model MiniMax-M2-Stable --key ... --group-id ...

Run:
    python examples/multi_step_research.py
"""

from __future__ import annotations

import asyncio

from rich.console import Console

from anthill.core.budget import Budget
from anthill.core.executor import ProgressEvent
from anthill.core.nation import Nation
from anthill.core.router import RouterConfig
from anthill.core.userconfig import load_config


console = Console()

REQUEST = (
    "Research what 'stigmergy' means in biology, then write a one-paragraph "
    "explanation a teenager would understand."
)


async def main() -> None:
    cfg = load_config()
    if not cfg.models:
        console.print(
            "[red]No models configured.[/red] This example needs a real model."
        )
        console.print(
            "Configure with [cyan]anthill model add deepseek "
            "--provider deepseek --model deepseek-chat --key sk-... "
            "--set-default[/cyan]."
        )
        return

    nation = Nation(
        name="research-demo",
        router_config=RouterConfig(exploration=0.10),
        scout_model="deepseek-chat",
    )
    nation.spawn(count=2, model="deepseek-chat")
    if cfg.find_model("minimax") is not None:
        nation.spawn(count=1, model="minimax")
        console.print("[dim]Nation: 2 DeepSeek + 1 MiniMax.[/dim]")
    else:
        console.print("[dim]Nation: 2 DeepSeek.[/dim]")

    console.print(f"[bold]Request[/bold] {REQUEST}")
    console.print()

    async def on_progress(event: ProgressEvent) -> None:
        st = event.subtask
        idx = event.index + 1
        if event.kind == "started":
            console.print(
                f"  [dim]·[/dim] [{idx}] [magenta]{st.task_type}[/magenta] "
                f"[dim]running…[/dim]"
            )
        elif event.kind == "attempt" and not event.success:
            console.print(
                f"    [yellow]retry[/yellow] attempt {event.attempt_number} failed, "
                f"rotating citizens…"
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

    result = await nation.ask(
        REQUEST,
        on_progress=on_progress,
        budget=Budget(max_cost_usd=0.10, max_seconds=60),
    )

    console.print()
    console.print("[bold]Plan[/bold]")
    for i, sub in enumerate(result.plan.subtasks, start=1):
        deps = (
            f" [dim](depends on: {', '.join(sub.depends_on)})[/dim]"
            if sub.depends_on
            else ""
        )
        console.print(f"  [cyan]#{i}[/cyan] [magenta]{sub.task_type}[/magenta]{deps}")

    console.print()
    console.print("[bold]Final answer[/bold]")
    console.print(result.final_output)

    if result.budget is not None:
        console.print()
        console.print(f"[dim]budget: {result.budget.summary}[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
