"""Plan cache — when a request looks like one we have planned before,
reuse the plan and skip the Scout call.

This is not the same as memoising the *answer*. Two similar requests
usually still need to run their subtasks with fresh prompts. What we
save is the Scout's planning round-trip — typically ~1 second and a few
hundred input tokens per ask. After a hundred asks the savings are real.

The lookup is by request similarity. We hash the request after light
normalisation (lowercase, collapse whitespace, strip punctuation). Two
requests that differ only in spacing or capitalisation hit the same
cache key.

We do not try to be clever about semantic similarity yet. That requires
embeddings, an extra service, and another failure mode. Exact-after-
normalisation is a good first pass — common queries like 'translate X
to Chinese' show up identically over and over and benefit immediately.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from anthill.core.scout import Plan, Subtask


_NORMALISE_RE = re.compile(r"[^\w\s]")


def normalise_request(request: str) -> str:
    s = request.strip().lower()
    s = _NORMALISE_RE.sub(" ", s)
    s = " ".join(s.split())
    return s


def plan_key(request: str) -> str:
    """Stable cache key from a normalised request."""
    norm = normalise_request(request)
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


@dataclass
class CachedPlan:
    key: str
    normalised_request: str
    plan: Plan
    hits: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "normalised_request": self.normalised_request,
            "plan": [
                {"task_type": s.task_type, "prompt": s.prompt, "depends_on": list(s.depends_on)}
                for s in self.plan.subtasks
            ],
            "hits": self.hits,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CachedPlan":
        return cls(
            key=data["key"],
            normalised_request=data["normalised_request"],
            plan=Plan(
                subtasks=[
                    Subtask(
                        task_type=s["task_type"],
                        prompt=s["prompt"],
                        depends_on=list(s.get("depends_on", [])),
                    )
                    for s in data["plan"]
                ]
            ),
            hits=data.get("hits", 0),
            created_at=data.get("created_at", time.time()),
            last_used=data.get("last_used", time.time()),
        )


def cache_path(nation_dir: Path) -> Path:
    return nation_dir / "plan_cache.json"


def load_cache(nation_dir: Path) -> dict[str, CachedPlan]:
    path = cache_path(nation_dir)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {k: CachedPlan.from_dict(v) for k, v in raw.items()}


def save_cache(cache: dict[str, CachedPlan], nation_dir: Path) -> None:
    nation_dir.mkdir(parents=True, exist_ok=True)
    cache_path(nation_dir).write_text(
        json.dumps({k: v.to_dict() for k, v in cache.items()}, indent=2, ensure_ascii=False)
    )


def lookup(request: str, cache: dict[str, CachedPlan]) -> CachedPlan | None:
    key = plan_key(request)
    cached = cache.get(key)
    if cached is None:
        return None
    cached.hits += 1
    cached.last_used = time.time()
    return cached


def remember(request: str, plan: Plan, cache: dict[str, CachedPlan]) -> CachedPlan:
    key = plan_key(request)
    cached = CachedPlan(
        key=key,
        normalised_request=normalise_request(request),
        plan=plan,
    )
    cache[key] = cached
    return cached
