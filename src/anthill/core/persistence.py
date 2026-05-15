"""Persist colony state — agents and pheromone trails — to disk.

Storage format is intentionally JSON for now. Easy to inspect, easy to diff
in git, easy to evolve. SQLite comes when we have a reason for it (likely
when trails grow past tens of thousands of rows or when concurrent writes
become a real issue).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from anthill.core.agent import Agent
from anthill.core.colony import Colony
from anthill.core.culture import load_culture, save_culture
from anthill.core.pheromone import PheromoneTrail, Trail


def colony_dir(home: Path, name: str) -> Path:
    return home / "colonies" / name


def save_colony(colony: Colony, home: Path) -> Path:
    """Write colony + pheromone state to ~/.anthill/colonies/<name>/."""
    directory = colony_dir(home, colony.name)
    directory.mkdir(parents=True, exist_ok=True)

    agents_data = [
        {
            "id": a.id,
            "model": a.model,
            "persona": a.persona,
            "private_memory": a.private_memory,
        }
        for a in colony.agents
    ]
    (directory / "agents.json").write_text(json.dumps(agents_data, indent=2))

    trails_data = [
        {
            "agent_id": t.agent_id,
            "task_type": t.task_type,
            "strength": t.strength,
            "last_updated": t.last_updated,
        }
        for t in colony.pheromones._trails.values()
    ]
    (directory / "pheromones.json").write_text(json.dumps(trails_data, indent=2))

    save_culture(colony.culture, directory)

    return directory


def load_colony(name: str, home: Path) -> Colony | None:
    """Read colony state from disk. Returns None if no colony with that name."""
    directory = colony_dir(home, name)
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
                last_updated=record.get("last_updated", time.time()),
            )

    culture = load_culture(directory)

    return Colony(name=name, agents=agents, pheromones=pheromones, culture=culture)
