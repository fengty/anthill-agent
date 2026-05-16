"""Turn raw token cost into a routing dimension.

`anthill costs` already aggregated usage and printed reports. That was
an open loop — informative but not connected to any decision. v0.4.3
closes the loop by treating cost-efficiency as one more dimension in
the same open-vocabulary catalog the LLM judge writes to.

Mechanics:
  - Every task attempt computes its USD cost from token counts + the
    model's per-million prices (core/costs.price_for).
  - The cost is normalized against a *rolling baseline median* kept
    per task_type — being twice as expensive as the running median
    knocks the score in half; being cheaper than median scores 1.0.
  - The normalized score lands on the trail as `dim_scores["cost"]`
    and gets registered in the catalog as the `cost` dimension.

Default behavior: `cost` is just another observed dimension. The
catalog records it; the router ignores it (weight 0 by default), so
v0.4.3 changes no routing decisions until the user explicitly says
`anthill values weight cost 1.0` (or similar).

That is the "mechanism not constraint" principle in action: the tool
provides the signal. Whether the nation actually trades quality for
cost is the user's call.
"""

from __future__ import annotations

from anthill.core.costs import price_for


# Dimension name the cost signal writes to. Lowercase + snake_case so
# `normalize_dim` is a no-op on it.
COST_DIMENSION = "cost"

# Smoothing constant for the per-task_type rolling baseline. Small
# enough that one anomalous cheap/expensive attempt doesn't drag the
# baseline around; large enough to catch real shifts (e.g. user adds a
# new provider with very different pricing).
BASELINE_ALPHA = 0.1


def compute_cost_usd(
    input_tokens: int,
    output_tokens: int,
    model: str,
) -> float:
    """The dollar cost of one attempt, given the model's price card."""
    in_per_m, out_per_m = price_for(model)
    return (
        input_tokens * in_per_m / 1_000_000
        + output_tokens * out_per_m / 1_000_000
    )


def cost_efficiency(cost: float, baseline: float | None) -> float:
    """Map an attempt's cost to a [0, 1] efficiency score vs. a baseline.

    The curve is intentionally not linear:
      cost == 0          →  1.0  (free / trivial)
      cost <= baseline   →  1.0  (at or under typical)
      cost == 2× baseline → 0.5  (twice as expensive ⇒ half the score)
      cost >= 3× baseline → 0.0  (three or more times the typical)

    When no baseline yet exists (first attempt of this kind), we return
    0.5 — neutral. That avoids unfairly boosting the very first attempt
    just because there's nothing to compare it to.
    """
    if baseline is None or baseline <= 0:
        return 0.5
    if cost <= 0:
        return 1.0
    ratio = cost / baseline
    if ratio <= 1.0:
        return 1.0
    if ratio >= 3.0:
        return 0.0
    # Linear from (1.0, 1.0) to (3.0, 0.0).
    return max(0.0, min(1.0, 1.0 - (ratio - 1.0) / 2.0))


def update_baseline(
    baselines: dict[str, float],
    task_type: str,
    cost: float,
    *,
    alpha: float = BASELINE_ALPHA,
) -> float:
    """Bring the per-task_type baseline toward a new observation via EWMA.

    Mutates `baselines` in place and returns the updated value. Negative
    costs are clamped to 0 so a bookkeeping bug can't poison the baseline.
    """
    cost = max(0.0, float(cost))
    prev = baselines.get(task_type)
    if prev is None:
        baselines[task_type] = cost
    else:
        baselines[task_type] = (1 - alpha) * prev + alpha * cost
    return baselines[task_type]


__all__ = [
    "COST_DIMENSION",
    "BASELINE_ALPHA",
    "compute_cost_usd",
    "cost_efficiency",
    "update_baseline",
]
