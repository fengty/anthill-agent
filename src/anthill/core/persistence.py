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
            "born_at": a.born_at,
            "retired_at": a.retired_at,
            "parent_id": a.parent_id,
            "generation": a.generation,
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
            kwargs = {
                "id": record["id"],
                "model": record.get("model", "deepseek-chat"),
                "persona": record.get("persona"),
                "private_memory": record.get("private_memory", {}),
            }
            # Older agents.json files predate lifecycle. Only pass these
            # fields when they exist so we don't override the dataclass
            # default-factory for born_at on legacy data.
            if "born_at" in record and record["born_at"] is not None:
                kwargs["born_at"] = float(record["born_at"])
            if "retired_at" in record:
                kwargs["retired_at"] = (
                    None if record["retired_at"] is None else float(record["retired_at"])
                )
            if "parent_id" in record:
                kwargs["parent_id"] = record["parent_id"]
            if "generation" in record and record["generation"] is not None:
                kwargs["generation"] = int(record["generation"])
            agents.append(Agent(**kwargs))

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
