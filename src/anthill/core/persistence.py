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
from anthill.core.values import DimensionCatalog


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
            "quarantined_at": a.quarantined_at,
            "quarantine_reason": a.quarantine_reason,
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
            "dim_scores": dict(t.dim_scores),
        }
        for t in nation.pheromones._trails.values()
    ]
    (directory / "pheromones.json").write_text(json.dumps(trails_data, indent=2))

    save_culture(nation.culture, directory)
    save_cache(nation.plan_cache, directory)
    (directory / "values.json").write_text(
        json.dumps(nation.dimension_catalog.to_dict(), indent=2)
    )
    # Cost baselines (v0.4.3) — per-task_type EWMA used by cost_signal.
    # Stored separately from values.json so the open-vocabulary catalog
    # stays semantically about "what good means", not "what good costs".
    (directory / "cost_baselines.json").write_text(
        json.dumps(dict(nation.cost_baselines), indent=2)
    )
    # Immune system policy (v0.5+). Only a flag for now — the sliding
    # windows themselves stay in memory and are rebuilt from history
    # when needed.
    (directory / "immune.json").write_text(
        json.dumps({"enabled": bool(nation.immune_enabled)}, indent=2)
    )

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
            if "quarantined_at" in record:
                kwargs["quarantined_at"] = (
                    None if record["quarantined_at"] is None
                    else float(record["quarantined_at"])
                )
            if "quarantine_reason" in record:
                kwargs["quarantine_reason"] = record["quarantine_reason"]
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
                dim_scores=dict(record.get("dim_scores") or {}),
            )

    culture = load_culture(directory)
    plan_cache = load_cache(directory)

    values_file = directory / "values.json"
    dimension_catalog = DimensionCatalog()
    if values_file.exists():
        try:
            dimension_catalog = DimensionCatalog.from_dict(
                json.loads(values_file.read_text())
            )
        except (OSError, json.JSONDecodeError):
            pass

    cost_baselines: dict[str, float] = {}
    cost_file = directory / "cost_baselines.json"
    if cost_file.exists():
        try:
            raw = json.loads(cost_file.read_text())
            if isinstance(raw, dict):
                for k, v in raw.items():
                    try:
                        cost_baselines[str(k)] = float(v)
                    except (TypeError, ValueError):
                        continue
        except (OSError, json.JSONDecodeError):
            pass

    immune_enabled = False
    immune_file = directory / "immune.json"
    if immune_file.exists():
        try:
            raw = json.loads(immune_file.read_text())
            immune_enabled = bool(raw.get("enabled", False))
        except (OSError, json.JSONDecodeError):
            pass

    return Nation(
        name=name,
        agents=agents,
        pheromones=pheromones,
        culture=culture,
        plan_cache=plan_cache,
        history_path=directory / "history.jsonl",
        dimension_catalog=dimension_catalog,
        cost_baselines=cost_baselines,
        immune_enabled=immune_enabled,
    )
