"""See the v0.3 lifecycle in one shot.

Spawns a small nation, simulates several rounds of work so pheromone
trails build up, then walks through the full architectural arc:

    audit   →   retire stale citizens
    rank    →   identify reproduction candidates
    reproduce →  spawn a mutated descendant
    family  →   show the resulting lineage

The simulated work uses an in-process fake provider so the example
runs offline. With real API keys set you can substitute real model
calls; the orchestration is identical.

Run:
    python examples/lifecycle_cycle.py
"""

from __future__ import annotations

import asyncio
import time

from rich.console import Console
from rich.table import Table

from anthill.core.agent import Agent, TaskResult
from anthill.core.lifecycle import (
    RetirementCriteria,
    audit_nation,
    retire_stale,
    snapshot_nation,
)
from anthill.core.nation import Nation
from anthill.core.reproduction import (
    ReproductionCriteria,
    descendants_of,
    rank_citizens,
    reproduce,
)


console = Console()


# A trivial in-process "provider" that returns deterministic outputs so
# this example doesn't require API keys. Wired by overriding
# Nation.run rather than the model layer — same code path the real
# tests use.
async def _fake_run(self, task_type, prompt, *, forbid=None):  # noqa: ANN001
    # For the demo we want the dormant citizen to *stay* dormant so the
    # audit has someone to flag. Skip anyone born more than a week ago —
    # in a real run the router's pheromone bias would do this naturally
    # once trails diverged.
    horizon = time.time() - 7 * 86_400
    candidates = [
        a for a in self.agents
        if not a.is_retired and a.born_at > horizon
    ]
    if forbid:
        candidates = [a for a in candidates if a.id not in forbid]
    if not candidates:
        raise RuntimeError("no citizens available")
    chosen = sorted(candidates, key=lambda a: a.id)[0]
    result = TaskResult(
        task_id=f"task-{int(time.time() * 1000)}",
        agent_id=chosen.id,
        task_type=task_type,
        output=f"<{task_type}>",
        success_score=1.0,
        duration_seconds=0.01,
        input_tokens=50,
        output_tokens=50,
    )
    self.pheromones.deposit(
        agent_id=result.agent_id,
        task_type=result.task_type,
        success_score=1.0,
    )
    self.culture.record(task_type)
    return result


def _print_roster(nation: Nation, title: str) -> None:
    snaps = snapshot_nation(nation, history=[])
    table = Table(title=title)
    table.add_column("ID", style="cyan")
    table.add_column("Model", style="magenta")
    table.add_column("Gen", justify="right")
    table.add_column("Parent", style="dim")
    table.add_column("Status")
    table.add_column("Trails", justify="right")
    table.add_column("Max strength", justify="right", style="green")
    color = {"active": "green", "quiet": "yellow", "untested": "dim", "retired": "red"}
    for s in snaps:
        a = next(x for x in nation.agents if x.id == s.agent_id)
        label = s.status_label()
        parent_short = (a.parent_id or "—")[:12]
        table.add_row(
            s.agent_id,
            s.model,
            str(a.generation),
            parent_short,
            f"[{color[label]}]{label}[/{color[label]}]",
            str(s.task_attempts),
            f"{s.max_strength:.2f}",
        )
    console.print(table)


async def main() -> None:
    Nation.run = _fake_run  # type: ignore[assignment]

    nation = Nation(name="lifecycle-demo")
    nation.spawn(count=3, model="deepseek-chat")
    # An extra citizen we'll deliberately leave dormant to trigger retirement.
    dormant = Agent(model="deepseek-chat")
    dormant.born_at = time.time() - 60 * 86_400  # 60 days old
    nation.agents.append(dormant)

    console.print("[bold]Round 1 — work happens, trails build[/bold]")
    for _ in range(6):
        await nation.run("research", "find something")
    for _ in range(4):
        await nation.run("summarize", "summarize it")
    _print_roster(nation, "After work")

    console.print()
    console.print("[bold]Audit — who is stale?[/bold]")
    report = audit_nation(nation, history=[], criteria=RetirementCriteria())
    if report.stale:
        for s in report.stale:
            idle = (
                f"{s.idle_days:.0f}d idle"
                if s.idle_days is not None
                else "never active"
            )
            console.print(
                f"  • {s.agent_id} — age {s.age_days:.0f}d, {idle}, "
                f"max strength {s.max_strength:.2f}"
            )
        retired = retire_stale(nation, history=[])
        console.print(f"[yellow]Retired {len(retired)} citizen(s).[/yellow]")
    else:
        console.print("  (no candidates — try increasing simulated time)")

    console.print()
    console.print("[bold]Rank — who qualifies to reproduce?[/bold]")
    scores = rank_citizens(nation, ReproductionCriteria())
    for s in scores[:3]:
        marker = "[green]✓[/green]" if s.qualifies else "[dim]·[/dim]"
        console.print(
            f"  {marker} {s.agent_id}  fitness {s.score:.2f}  "
            f"[dim]{s.reason()}[/dim]"
        )

    fittest = next((s for s in scores if s.qualifies), None)
    if fittest is not None:
        console.print()
        console.print(f"[bold]Reproduce[/bold] the fittest citizen ({fittest.agent_id})")
        parent = nation.find_agent(fittest.agent_id)
        assert parent is not None
        lineage = reproduce(nation, parent)
        console.print(
            f"  → child {lineage.child.id} born "
            f"(mutation: {lineage.mutation.name})"
        )
        for note in lineage.notes:
            console.print(f"     • {note}")

        descs = descendants_of(nation, parent.id)
        console.print(f"  parent now has {len(descs)} descendant(s).")

    console.print()
    _print_roster(nation, "Nation after the lifecycle pass")


if __name__ == "__main__":
    asyncio.run(main())
