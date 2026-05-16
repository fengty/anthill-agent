"""See specialization emerge.

Spawns a small nation of identical agents (or a mix of providers), runs
several rounds of two different task types, and prints the pheromone map.

You should see one citizen dominate "translate" and another dominate
"explain" purely from random cold-start choices followed by trail
reinforcement.

Requires at least one configured model. Set them up once with:

    anthill model add deepseek --provider deepseek \\
      --model deepseek-chat --key sk-... --set-default

Optional second provider so the router actually has a choice:

    anthill model add minimax --provider minimax \\
      --model MiniMax-M2-Stable --key ... --group-id ...

Run:
    python examples/emergent_specialization.py
"""

from __future__ import annotations

import asyncio
from rich.console import Console
from rich.table import Table

from anthill.core.nation import Nation
from anthill.core.router import RouterConfig
from anthill.core.userconfig import load_config

console = Console()

TASKS = [
    ("translate", "Translate 'hello world' to Japanese. Return only the translation."),
    ("translate", "Translate 'good morning' to French. Return only the translation."),
    ("translate", "Translate 'thank you' to German. Return only the translation."),
    ("translate", "Translate 'see you tomorrow' to Spanish. Return only the translation."),
    ("explain", "Explain stigmergy in one sentence."),
    ("explain", "Explain pheromone trails in one sentence."),
    ("explain", "Explain ant nest optimization in one sentence."),
    ("explain", "Explain swarm intelligence in one sentence."),
]


def print_trails(nation: Nation, title: str) -> None:
    table = Table(title=title)
    table.add_column("Citizen", style="cyan")
    table.add_column("Model", style="dim")
    table.add_column("Task Type", style="magenta")
    table.add_column("Strength", style="green", justify="right")

    model_by_id = {a.id: a.model for a in nation.agents}
    trails = sorted(nation.pheromones.trails(), key=lambda t: t.strength, reverse=True)
    for t in trails:
        table.add_row(t.agent_id, model_by_id.get(t.agent_id, "?"), t.task_type, f"{t.strength:.2f}")
    console.print(table)


async def main() -> None:
    cfg = load_config()
    if not cfg.models:
        console.print(
            "[red]No models configured.[/red] Run "
            "[cyan]anthill model add deepseek --provider deepseek "
            "--model deepseek-chat --key sk-... --set-default[/cyan] first."
        )
        return

    nation = Nation(name="demo", router_config=RouterConfig(exploration=0.10))

    # DeepSeek is the default route; MiniMax slots in only when configured.
    nation.spawn(count=2, model="deepseek-chat")
    if cfg.find_model("minimax") is not None:
        nation.spawn(count=2, model="minimax")
        console.print("[dim]Spawned 2 DeepSeek + 2 MiniMax citizens.[/dim]")
    else:
        nation.spawn(count=2, model="deepseek-chat")
        console.print(
            "[dim]Spawned 4 DeepSeek citizens "
            "(add a second provider with `anthill model add minimax ...`).[/dim]"
        )

    console.print()
    for i, (task_type, prompt) in enumerate(TASKS, start=1):
        result = await nation.run(task_type, prompt)
        chosen = next((a for a in nation.agents if a.id == result.agent_id), None)
        model = chosen.model if chosen else "?"
        console.print(
            f"[{i:2}] {task_type:10s} -> {result.agent_id} ({model})  "
            f"score={result.success_score:.1f}  {result.duration_seconds:.1f}s"
        )

    console.print()
    print_trails(nation, "Pheromone trails after the run")


if __name__ == "__main__":
    asyncio.run(main())
