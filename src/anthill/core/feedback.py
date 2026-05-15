"""Feedback — the king's verdict, applied to pheromone trails.

Without feedback, pheromones reflect only "did the API call succeed."
That's the wrong signal long-term: an answer can be syntactically fine
and still useless. The king is the ground truth.

This module records the king's rating of the last ask and translates it
into pheromone adjustments — strengthening trails the king liked, eroding
trails the king rejected.

Two design notes:

- We persist the *last* ask, not every ask. That keeps the rating
  workflow ergonomic — `anthill rate up` should refer to "the last
  thing you saw." A full history lives in v0.0.11.

- A rating applies to every citizen+task_type pair in the ask. If a
  three-step plan went well, all three steps get reinforced. If the
  user thumbs-down, all three lose strength. We can refine to per-step
  rating later, but rating the whole composite first matches how
  humans actually evaluate.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from anthill.core.pheromone import PheromoneTrail


Rating = Literal["up", "down"]


@dataclass
class AskRecord:
    """A persistent record of a recent ask, used as the target of `rate`."""

    request: str
    timestamp: float
    pairs: list[tuple[str, str]]  # (agent_id, task_type) per executed subtask

    def to_dict(self) -> dict:
        return {
            "request": self.request,
            "timestamp": self.timestamp,
            "pairs": [list(p) for p in self.pairs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AskRecord":
        return cls(
            request=data["request"],
            timestamp=data["timestamp"],
            pairs=[tuple(p) for p in data["pairs"]],
        )


def save_last_ask(record: AskRecord, nation_dir: Path) -> None:
    nation_dir.mkdir(parents=True, exist_ok=True)
    (nation_dir / "last_ask.json").write_text(json.dumps(record.to_dict(), indent=2))


def load_last_ask(nation_dir: Path) -> AskRecord | None:
    path = nation_dir / "last_ask.json"
    if not path.exists():
        return None
    return AskRecord.from_dict(json.loads(path.read_text()))


def apply_rating(
    rating: Rating,
    record: AskRecord,
    pheromones: PheromoneTrail,
    *,
    weight: float = 2.0,
) -> int:
    """Apply the king's verdict to the pheromone map.

    `weight` is the score multiplier — a rating is a much stronger signal
    than the routine success check, so we give it more impact than a
    single successful task would normally deposit. Returns the number of
    (agent, task_type) trails touched.
    """
    if rating == "up":
        score = weight
    else:
        score = -weight

    touched = 0
    for agent_id, task_type in record.pairs:
        if rating == "up":
            pheromones.deposit(agent_id, task_type, success_score=score)
        else:
            # Negative ratings erode rather than just stop reinforcing.
            # Use the deposit interface but with a multiplier that pushes
            # the trail toward zero. The existing floor at zero protects
            # us from going negative.
            trail = pheromones._trails.get((agent_id, task_type))
            if trail is not None:
                trail.strength = max(0.0, trail.strength - weight)
                trail.last_updated = time.time()
        touched += 1
    return touched
