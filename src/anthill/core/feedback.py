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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from anthill.core.values import DimensionCatalog

from anthill.core.pheromone import PheromoneTrail


Rating = Literal["up", "down"]


@dataclass
class AskRecord:
    """A persistent record of a recent ask, used as the target of `rate`.

    `final_output` is preserved so a rating can ALSO save an exemplar of
    "what the king approved" or "what the king rejected." Style learning
    in v0.0.10 mines these exemplars to suggest house_style refinements.
    """

    request: str
    timestamp: float
    pairs: list[tuple[str, str]]  # (agent_id, task_type) per executed subtask
    final_output: str = ""

    def to_dict(self) -> dict:
        return {
            "request": self.request,
            "timestamp": self.timestamp,
            "pairs": [list(p) for p in self.pairs],
            "final_output": self.final_output,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AskRecord":
        return cls(
            request=data["request"],
            timestamp=data["timestamp"],
            pairs=[tuple(p) for p in data["pairs"]],
            final_output=data.get("final_output", ""),
        )


@dataclass
class Exemplar:
    """A rated output preserved for style learning."""

    rating: Rating
    request: str
    output: str
    timestamp: float


def exemplars_path(nation_dir: Path) -> Path:
    return nation_dir / "exemplars.json"


def append_exemplar(exemplar: Exemplar, nation_dir: Path) -> None:
    nation_dir.mkdir(parents=True, exist_ok=True)
    path = exemplars_path(nation_dir)
    existing = json.loads(path.read_text()) if path.exists() else []
    existing.append(
        {
            "rating": exemplar.rating,
            "request": exemplar.request,
            "output": exemplar.output,
            "timestamp": exemplar.timestamp,
        }
    )
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))


def load_exemplars(nation_dir: Path) -> list[Exemplar]:
    path = exemplars_path(nation_dir)
    if not path.exists():
        return []
    return [
        Exemplar(
            rating=item["rating"],
            request=item["request"],
            output=item["output"],
            timestamp=item["timestamp"],
        )
        for item in json.loads(path.read_text())
    ]


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
    dim_scores: dict[str, float] | None = None,
    catalog: "DimensionCatalog | None" = None,
) -> int:
    """Apply the king's verdict to the pheromone map.

    `weight` is the score multiplier — a rating is a much stronger signal
    than the routine success check, so we give it more impact than a
    single successful task would normally deposit. Returns the number of
    (agent, task_type) trails touched.

    `dim_scores` (v0.4.2+) lets the rating carry per-dimension judgments:
    `{"correctness": 1.0, "conciseness": 0.0}` means "great on accuracy
    but way too verbose." When provided:
      - each dim is recorded on every (citizen, task_type) trail
        involved in this ask (so the router can later weight by it)
      - the DimensionCatalog (if given) auto-registers the names —
        the user's vocabulary is on the same footing as the judge's
    Combined with rating up/down, this lets a user say
    `anthill rate up --dim conciseness=down` and have the overall
    pheromone still grow while penalizing one specific dimension.
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
        # Per-dimension feedback: drop the scores onto each trail and
        # register the names in the catalog so they immediately become
        # weightable. Works whether the overall rating was up or down —
        # they are orthogonal axes.
        if dim_scores:
            pheromones.record_dimensions(agent_id, task_type, dim_scores)
            if catalog is not None:
                for dim_name, score_value in dim_scores.items():
                    catalog.observe(dim_name, score=score_value)
        touched += 1
    return touched
