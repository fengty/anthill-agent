"""History — every ask gets a permanent record.

Until now, the nation kept only the *last* ask (so 'anthill rate' had a
target). That's enough for immediate feedback but loses everything past
the most recent request. A nation that cannot remember what it has done
cannot grow institutional memory.

Each ask appends an HistoryEntry to history.jsonl — newline-delimited
JSON, easy to inspect, easy to grep, never rewritten. The CLI exposes:

    anthill history          list recent entries
    anthill history show ID  print the full trace for one entry
    anthill history search Q grep across requests

The id is the first 8 chars of a sha256 of (request + timestamp), so
listing shows short stable handles. No more guessing which one was
yesterday.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HistoryEntry:
    id: str
    timestamp: float
    request: str
    plan: list[dict]  # serialized subtasks: {task_type, depends_on}
    outcomes: list[dict] = field(default_factory=list)  # status + final output per subtask

    @staticmethod
    def make_id(request: str, timestamp: float) -> str:
        return hashlib.sha256(f"{request}{timestamp}".encode()).hexdigest()[:8]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "request": self.request,
            "plan": self.plan,
            "outcomes": self.outcomes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            id=data["id"],
            timestamp=data["timestamp"],
            request=data["request"],
            plan=data.get("plan", []),
            outcomes=data.get("outcomes", []),
        )


def history_path(nation_dir: Path) -> Path:
    return nation_dir / "history.jsonl"


def append_history(entry: HistoryEntry, nation_dir: Path) -> None:
    nation_dir.mkdir(parents=True, exist_ok=True)
    with history_path(nation_dir).open("a") as f:
        f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")


def load_history(nation_dir: Path, *, limit: int | None = None) -> list[HistoryEntry]:
    path = history_path(nation_dir)
    if not path.exists():
        return []
    entries: list[HistoryEntry] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(HistoryEntry.from_dict(json.loads(line)))
    if limit is not None:
        entries = entries[-limit:]
    return entries


def find_by_id(entry_id: str, nation_dir: Path) -> HistoryEntry | None:
    for entry in load_history(nation_dir):
        if entry.id.startswith(entry_id):  # prefix match — short ids
            return entry
    return None


def search_history(query: str, nation_dir: Path) -> list[HistoryEntry]:
    needle = query.lower()
    return [e for e in load_history(nation_dir) if needle in e.request.lower()]


def build_entry_from_ask(
    request: str,
    plan_subtasks: list,  # list of Subtask
    outcomes: list,  # list of SubtaskOutcome
) -> HistoryEntry:
    ts = time.time()
    return HistoryEntry(
        id=HistoryEntry.make_id(request, ts),
        timestamp=ts,
        request=request,
        plan=[
            {"task_type": s.task_type, "depends_on": list(s.depends_on)}
            for s in plan_subtasks
        ],
        outcomes=[
            {
                "task_type": o.subtask.task_type,
                "status": o.status,
                "attempts": len(o.attempts),
                "final_output": o.output if o.status == "ok" else None,
                "skip_reason": o.skip_reason,
                # agent_id of the final attempt — None when the subtask was
                # skipped before any citizen ran it. Used by the lifecycle
                # auditor (v0.3.0) to credit recent activity to specific
                # citizens; older history files predate this field and the
                # auditor falls back to pheromone timestamps for them.
                "agent_id": o.final.agent_id if o.final is not None else None,
                # Structured failure attribution per attempt (v0.5+). Stored
                # as a list of FailureReason value strings; len matches
                # the number of attempts. Successful attempts contribute None.
                "failure_reasons": [
                    getattr(a, "failure_reason", None) for a in o.attempts
                ],
            }
            for o in outcomes
        ],
    )
