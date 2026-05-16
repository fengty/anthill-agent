"""Inflight asks — checkpoint a multi-subtask request so a crash isn't fatal.

A three-step ask can take a minute. If the process dies halfway — Ctrl-C,
laptop closed, network blip — every subtask that already succeeded is
wasted: the user pays for the same tokens again on the next attempt.

The executor already emits `kind="finished"` events as each subtask
resolves. Nation hooks into that stream and writes the OK outcomes to a
small JSON file under `<nation>/inflight/<ask_id>.json`. On clean
completion of the whole ask, the file is removed. When the file
survives (the process didn't reach the cleanup), `anthill resume <id>`
can read it back, pre-seed the executor's outcomes for the completed
steps, and only run the missing ones.

We deliberately persist only OK outcomes:
- Failed outcomes are retried fresh on resume — that's the whole point
  of "resume", not "remember which steps were broken".
- Skipped outcomes were skipped because a dependency failed; on resume
  the dependency runs again, and the skip is no longer correct.

The on-disk shape is JSON, not pickle, so a future Anthill can read a
file written by an older one. Each schema bump should land alongside a
read-time migration here.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from anthill.core.scout import Plan, Subtask


SCHEMA_VERSION = 1


@dataclass
class CompletedStep:
    """Just enough of a SubtaskOutcome to resume from disk.

    We keep the final output (needed to build context for downstream
    subtasks) plus the bookkeeping the user will want to see in
    `inflight show` (timing, attempts, the agent that succeeded). We do
    NOT keep every TaskResult attempt — a resumed run starts fresh on
    failed steps and doesn't need to relitigate the old retry trace.
    """

    index: int
    task_type: str
    output: str
    agent_id: str
    started_at: float
    ended_at: float
    attempts: int = 1
    success_score: float = 1.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "task_type": self.task_type,
            "output": self.output,
            "agent_id": self.agent_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "attempts": self.attempts,
            "success_score": self.success_score,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CompletedStep":
        return cls(
            index=int(data["index"]),
            task_type=str(data["task_type"]),
            output=str(data.get("output", "")),
            agent_id=str(data.get("agent_id", "")),
            started_at=float(data.get("started_at", 0.0)),
            ended_at=float(data.get("ended_at", 0.0)),
            attempts=int(data.get("attempts", 1)),
            success_score=float(data.get("success_score", 1.0)),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
        )


@dataclass
class InflightAsk:
    """A request that started but may not have finished.

    Holds the original request, the Scout's plan, and every subtask that
    has reached status='ok' so far. The executor can rebuild everything
    it needs to resume from this.
    """

    ask_id: str
    request: str
    started_at: float
    plan: Plan
    completed: list[CompletedStep] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @staticmethod
    def make_id(request: str, timestamp: float) -> str:
        """8-char prefix of sha256(request+timestamp). Same shape as history ids."""
        return hashlib.sha256(f"{request}{timestamp}".encode()).hexdigest()[:8]

    @classmethod
    def new(cls, request: str, plan: Plan) -> "InflightAsk":
        ts = time.time()
        return cls(
            ask_id=cls.make_id(request, ts),
            request=request,
            started_at=ts,
            plan=plan,
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "ask_id": self.ask_id,
            "request": self.request,
            "started_at": self.started_at,
            "plan": [
                {
                    "task_type": s.task_type,
                    "prompt": s.prompt,
                    "depends_on": list(s.depends_on),
                }
                for s in self.plan.subtasks
            ],
            "completed": [c.to_dict() for c in self.completed],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InflightAsk":
        plan = Plan(
            subtasks=[
                Subtask(
                    task_type=str(s["task_type"]),
                    prompt=str(s["prompt"]),
                    depends_on=list(s.get("depends_on", [])),
                )
                for s in data.get("plan", [])
            ]
        )
        return cls(
            ask_id=str(data["ask_id"]),
            request=str(data["request"]),
            started_at=float(data.get("started_at", time.time())),
            plan=plan,
            completed=[CompletedStep.from_dict(c) for c in data.get("completed", [])],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )

    def record_completed(self, step: CompletedStep) -> None:
        """Append (or replace if the same index was already recorded)."""
        # The "replace" path is defensive — execute_plan shouldn't checkpoint
        # the same index twice, but a future bug shouldn't silently corrupt
        # the file by appending a duplicate.
        for i, existing in enumerate(self.completed):
            if existing.index == step.index:
                self.completed[i] = step
                return
        self.completed.append(step)

    def completed_indices(self) -> set[int]:
        return {c.index for c in self.completed}

    def latest_by_type(self) -> dict[str, str]:
        """For each task_type, the output of its most-recent completed step.

        This matches the executor's context-resolution rule (latest matching
        earlier subtask wins) and is what build_context_block needs.
        """
        out: dict[str, str] = {}
        # Walk in order so later writes overwrite earlier ones for the same type.
        for step in sorted(self.completed, key=lambda c: c.index):
            out[step.task_type] = step.output
        return out


# --- on-disk I/O -----------------------------------------------------------

def inflight_dir(nation_dir: Path) -> Path:
    return nation_dir / "inflight"


def inflight_path(nation_dir: Path, ask_id: str) -> Path:
    return inflight_dir(nation_dir) / f"{ask_id}.json"


def save_inflight(ask: InflightAsk, nation_dir: Path) -> Path:
    """Write atomically (tmpfile + rename) so a mid-write crash never corrupts.

    Atomic rename is the cheapest crash-safe write on POSIX. The
    alternative — overwrite-in-place — can leave half-written JSON on
    disk, which `load_inflight` would reject on the next run.
    """
    target_dir = inflight_dir(nation_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = inflight_path(nation_dir, ask.ask_id)

    payload = json.dumps(ask.to_dict(), ensure_ascii=False, indent=2)
    fd, tmp_path = tempfile.mkstemp(prefix=f"{ask.ask_id}.", suffix=".tmp", dir=str(target_dir))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup; never leave .tmp files behind.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def load_inflight(ask_id: str, nation_dir: Path) -> InflightAsk | None:
    """Prefix match so users can type the first few chars of an ask_id."""
    for path in list_inflight_paths(nation_dir):
        stem = path.stem
        if stem == ask_id or stem.startswith(ask_id):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                return None
            return InflightAsk.from_dict(data)
    return None


def list_inflight(nation_dir: Path) -> list[InflightAsk]:
    """Every checkpoint we can read, newest first."""
    asks: list[InflightAsk] = []
    for path in list_inflight_paths(nation_dir):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # A corrupt checkpoint should not break `inflight list`; skip it.
            continue
        asks.append(InflightAsk.from_dict(data))
    asks.sort(key=lambda a: a.started_at, reverse=True)
    return asks


def list_inflight_paths(nation_dir: Path) -> list[Path]:
    d = inflight_dir(nation_dir)
    if not d.exists():
        return []
    return [p for p in d.iterdir() if p.suffix == ".json"]


def clear_inflight(ask_id: str, nation_dir: Path) -> bool:
    """Remove the checkpoint file for `ask_id`. Returns True if a file was deleted."""
    for path in list_inflight_paths(nation_dir):
        if path.stem == ask_id or path.stem.startswith(ask_id):
            try:
                path.unlink()
                return True
            except OSError:
                return False
    return False
