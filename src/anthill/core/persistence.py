"""Persist Nation state — agents, pheromones, culture — to disk.

Storage is plain JSON + markdown for now. Easy to inspect, easy to diff
in git, easy to evolve. A real database goes in when the volume justifies
it; right now files-on-disk is exactly the right tradeoff.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from anthill.core.agent import Agent
from anthill.core.culture import load_culture, save_culture
from anthill.core.nation import Nation
from anthill.core.pheromone import PheromoneTrail, Trail
from anthill.core.plan_cache import load_cache, save_cache


def nation_dir(home: Path, name: str) -> Path:
    return home / "nations" / name


def save_nation(nation: Nation, home: Path) -> Path:
    """Write nation + pheromone + culture state to ~/.anthill/nations/<name>/."""
    directory = nation_dir(home, nation.name)
    directory.mkdir(parents=True, exist_ok=True)

    agents_data = [
        {
            "id": a.id,
            "model": a.model,
            "persona": a.persona,
            "private_memory": a.private_memory,
        }
        for a in nation.agents
    ]
    (directory / "agents.json").write_text(json.dumps(agents_data, indent=2))

    trails_data = [
        {
            "agent_id": t.agent_id,
            "task_type": t.task_type,
            "strength": t.strength,
            "alarm": t.alarm,
            "last_updated": t.last_updated,
        }
        for t in nation.pheromones._trails.values()
    ]
    (directory / "pheromones.json").write_text(json.dumps(trails_data, indent=2))

    save_culture(nation.culture, directory)
    save_cache(nation.plan_cache, directory)

    return directory


def load_nation(name: str, home: Path) -> Nation | None:
    """Read nation state from disk. Returns None if no nation with that name."""
    directory = nation_dir(home, name)
    if not directory.exists():
        return None

    agents_file = directory / "agents.json"
    pheromones_file = directory / "pheromones.json"

    agents: list[Agent] = []
    if agents_file.exists():
        for record in json.loads(agents_file.read_text()):
            agents.append(
                Agent(
                    id=record["id"],
                    model=record.get("model", "deepseek-chat"),
                    persona=record.get("persona"),
                    private_memory=record.get("private_memory", {}),
                )
            )

    pheromones = PheromoneTrail()
    if pheromones_file.exists():
        for record in json.loads(pheromones_file.read_text()):
            key = (record["agent_id"], record["task_type"])
            pheromones._trails[key] = Trail(
                agent_id=record["agent_id"],
                task_type=record["task_type"],
                strength=record["strength"],
                alarm=record.get("alarm", 0.0),
                last_updated=record.get("last_updated", time.time()),
            )

    culture = load_culture(directory)
    plan_cache = load_cache(directory)

    return Nation(
        name=name,
        agents=agents,
        pheromones=pheromones,
        culture=culture,
        plan_cache=plan_cache,
        history_path=directory / "history.jsonl",
    )
