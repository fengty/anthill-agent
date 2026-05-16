"""Open-vocabulary value dimensions — let the model name what "good" means.

Earlier versions had a single `success_score: float` for "how good was
this attempt." That collapsed every notion of quality — correctness,
brevity, tone, depth, citation discipline — into one number.

Hard-coding a fixed set of dimensions (correctness/conciseness/depth/tone)
would be the obvious fix. We are not doing that. The whole point of
Anthill being a *mechanism* rather than a frozen contract is that the
right dimensions emerge from the models doing the work. v0.4 ships the
plumbing for arbitrary dimensions; the dimensions themselves are
discovered by the LLM judge and by the user's own `anthill rate` calls.

A `DimensionCatalog` is the nation's accumulated vocabulary for what
"good" means to its user. It grows the same way `task_type` grows in
culture.py: the first time anything (judge, user, plugin) mentions a
new dimension, it's auto-registered with whatever description came
along. The catalog persists with the rest of the nation state.

Two things this module does NOT do:
  - It does NOT pre-seed dimensions. A brand-new nation starts empty,
    so the model is free to invent labels that fit the work.
  - It does NOT enforce a closed vocabulary. The judge can name a
    dimension Anthill has never seen, and the only thing that
    happens is a new row in the catalog.

What this module DOES do:
  - Holds the canonical name + description + observation stats.
  - Normalizes incoming dimension names (lowercase, snake_case) so
    `Correctness` and `correctness` end up as one.
  - Exposes a single `observe()` entry point that's idempotent.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


def normalize_dim(name: str) -> str:
    """Lowercase + snake_case so dimension naming variance doesn't fragment trails.

    `Correctness`, `correctness`, `Correct-ness` all map to `correctness`.
    Leaves digits intact. Strips anything that isn't alnum or underscore.
    """
    s = name.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


@dataclass
class Dimension:
    """One named axis along which "good" is measured for this nation."""

    name: str
    description: str = ""  # filled by whoever first observed it
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    observations: int = 0
    # Running average of scores ever reported for this dimension. Cheap
    # proxy for "how well does this nation currently score on X."
    avg_score: float = 0.0

    def update_score(self, score: float) -> None:
        """Incremental mean update — no need to hold every datapoint."""
        if self.observations == 0:
            self.avg_score = score
        else:
            # Exponentially weighted enough to react to recent shifts,
            # cheap enough to compute online.
            alpha = 1.0 / max(1, min(self.observations, 50))
            self.avg_score = (1 - alpha) * self.avg_score + alpha * score
        self.observations += 1
        self.last_seen = time.time()


@dataclass
class DimensionCatalog:
    """The nation's open-vocabulary set of value dimensions."""

    dimensions: dict[str, Dimension] = field(default_factory=dict)
    # Per-dimension weight when the router combines scores into a routing
    # signal. Missing entries default to 1.0. Users can override via the
    # `anthill values weight` command.
    weights: dict[str, float] = field(default_factory=dict)

    def observe(
        self,
        name: str,
        *,
        score: float | None = None,
        description: str = "",
    ) -> Dimension:
        """Register the dimension (or refresh it) and optionally record a score.

        Returns the canonical Dimension entry so callers can read its
        normalized name back. Description is only written on first
        observation — later observations keep the original wording so
        late-arriving rephrases don't clobber the established meaning.
        """
        key = normalize_dim(name)
        if not key:
            raise ValueError(f"dimension name {name!r} normalizes to empty")
        dim = self.dimensions.get(key)
        if dim is None:
            dim = Dimension(name=key, description=description)
            self.dimensions[key] = dim
        elif description and not dim.description:
            # Fill in description if we got one and didn't have one before.
            dim.description = description
        if score is not None:
            score = max(0.0, min(1.0, float(score)))
            dim.update_score(score)
        return dim

    def weight(self, name: str) -> float:
        return self.weights.get(normalize_dim(name), 1.0)

    def set_weight(self, name: str, value: float) -> None:
        self.weights[normalize_dim(name)] = float(value)

    def reset_weights(self) -> None:
        self.weights.clear()

    def known(self) -> list[str]:
        """Canonical names of every dimension we've ever observed."""
        return sorted(self.dimensions.keys())

    def to_dict(self) -> dict:
        return {
            "dimensions": {
                k: {
                    "name": v.name,
                    "description": v.description,
                    "first_seen": v.first_seen,
                    "last_seen": v.last_seen,
                    "observations": v.observations,
                    "avg_score": v.avg_score,
                }
                for k, v in self.dimensions.items()
            },
            "weights": dict(self.weights),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DimensionCatalog":
        out = cls()
        for key, raw in (data.get("dimensions") or {}).items():
            out.dimensions[key] = Dimension(
                name=str(raw.get("name", key)),
                description=str(raw.get("description", "")),
                first_seen=float(raw.get("first_seen", time.time())),
                last_seen=float(raw.get("last_seen", time.time())),
                observations=int(raw.get("observations", 0)),
                avg_score=float(raw.get("avg_score", 0.0)),
            )
        for k, v in (data.get("weights") or {}).items():
            try:
                out.weights[normalize_dim(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out


# --- score aggregation -----------------------------------------------------


def aggregate(
    scores: dict[str, float],
    catalog: DimensionCatalog | None = None,
) -> float:
    """Collapse a multi-dim score dict back into a single [0, 1] signal.

    Used wherever legacy code expects a scalar (router decay math,
    pheromone deposit amount). If a catalog is given, per-dimension
    weights from `catalog.weights` are honored; otherwise it's a flat
    average. An empty scores dict returns 0.0 — that means "no quality
    information present", and treating it as failure would be wrong.
    Callers should check `if scores` before calling.
    """
    if not scores:
        return 0.0
    if catalog is None:
        return sum(scores.values()) / len(scores)
    total = 0.0
    weight_sum = 0.0
    for name, value in scores.items():
        w = catalog.weight(name)
        total += w * value
        weight_sum += w
    if weight_sum <= 0:
        return sum(scores.values()) / len(scores)
    return total / weight_sum


__all__ = [
    "Dimension",
    "DimensionCatalog",
    "aggregate",
    "normalize_dim",
]
