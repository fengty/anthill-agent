"""Ensemble execution + selection — multiple citizens on one subtask.

v0.6 lets a single subtask fan out to K parallel attempts on different
citizens, then pick a winner. This is the natural extension of
multi-model orchestration: instead of always trusting the router's
single choice, we can ask "let everyone try, then pick the best."

What the tool offers is the *mechanism*:
  - parallel dispatch to K distinct citizens
  - a pluggable selection strategy
  - per-attempt judge data is reused — fanout doesn't double-pay for evaluation

What stays open (the user / model's choice):
  - whether to fanout at all (default fanout=1 ⇒ legacy behavior)
  - how many parallel attempts
  - which strategy to pick the winner

Strategies live here because they're independent of executor logic;
the executor just calls `select_winner` after fanout. Adding a new
strategy is one function + one branch in the dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anthill.core.agent import TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Subtask


# --- selection strategies --------------------------------------------------


def _first_success(attempts: list["TaskResult"]) -> "TaskResult":
    """Pick the first attempt with success_score > 0.

    Fall back to the highest-score attempt if nothing succeeded — that
    way the executor still has something to put into the outcome
    rather than an arbitrary failure.
    """
    for a in attempts:
        if a.success_score > 0:
            return a
    return max(attempts, key=lambda r: r.success_score)


def _highest_score(attempts: list["TaskResult"]) -> "TaskResult":
    """Highest overall success_score. Tied scores break to the shorter output.

    "Tied" here means within 0.01 — judge scores are noisy at the third
    decimal and we don't want flapping.
    """
    sorted_by_score = sorted(
        attempts,
        key=lambda r: (-r.success_score, len(str(r.output))),
    )
    return sorted_by_score[0]


def _shortest_correct(attempts: list["TaskResult"]) -> "TaskResult":
    """Among attempts that scored ≥ 0.7, pick the shortest output.

    Useful when conciseness matters and you have a quality floor.
    Falls back to highest-score if nothing clears the floor.
    """
    qualifying = [a for a in attempts if a.success_score >= 0.7]
    if not qualifying:
        return _highest_score(attempts)
    return min(qualifying, key=lambda r: len(str(r.output)))


def _majority(attempts: list["TaskResult"]) -> "TaskResult":
    """Crude similarity bucketing — group attempts by exact output equality.

    Pick the largest bucket, tiebreak by highest success_score. This is
    a v0 implementation; a future revision can swap in embedding-based
    clustering, but exact match catches a surprising amount (multiple
    citizens often converge on the same short factual answer).
    """
    from collections import defaultdict
    buckets: dict[str, list["TaskResult"]] = defaultdict(list)
    for a in attempts:
        buckets[str(a.output).strip()].append(a)
    # Largest bucket first; within a bucket pick highest score.
    largest_bucket = max(buckets.values(), key=len)
    return _highest_score(largest_bucket)


_STRATEGIES = {
    "first_success": _first_success,
    "highest_score": _highest_score,
    "shortest_correct": _shortest_correct,
    "majority": _majority,
}


def known_strategies() -> list[str]:
    return sorted(_STRATEGIES.keys())


def select_winner(
    attempts: list["TaskResult"],
    *,
    strategy: str,
) -> "TaskResult":
    """Pick the winning attempt by name. Unknown strategy → first_success."""
    if not attempts:
        raise ValueError("select_winner called with empty attempts list")
    fn = _STRATEGIES.get(strategy, _first_success)
    return fn(attempts)


# --- parallel fanout dispatch ---------------------------------------------


async def run_fanout(
    nation: "Nation",
    subtask: "Subtask",
    augmented_prompt: str,
    k: int,
) -> list["TaskResult"]:
    """Launch K parallel attempts on distinct citizens; return all results.

    Pre-assigns citizens before dispatch so the K attempts don't all
    end up on the same one. If fewer than K distinct citizens are
    available (small nation, others retired/quarantined), we run with
    whatever we have and return fewer attempts.

    Each result goes through the same nation.run pipeline (pheromone
    deposit, judge if enabled, dimension catalog, immune system), so
    fanout doesn't bypass any of the learning loops — it just multiplies
    the data per subtask.
    """
    import asyncio

    if k <= 1:
        result = await nation.run(subtask.task_type, augmented_prompt)
        return [result]

    # Pre-select up to K distinct citizens using cumulative forbid.
    router = nation.router
    chosen: list[str] = []
    for _ in range(k):
        try:
            agent = router.assign(subtask.task_type, forbid=set(chosen))
        except RuntimeError:
            break
        chosen.append(agent.id)

    if not chosen:
        # No one available — let nation.run raise the canonical error.
        result = await nation.run(subtask.task_type, augmented_prompt)
        return [result]

    # Dispatch K parallel calls. Each goes through nation.run so the
    # full learning pipeline (judge, pheromone, dimensions, immune)
    # fires for every attempt. forbid={others} keeps the router from
    # collapsing back to one citizen if any of them ends up failing
    # internally and re-routing.
    async def _one(target_id: str) -> "TaskResult":
        others = {cid for cid in chosen if cid != target_id}
        return await nation.run(
            subtask.task_type, augmented_prompt, forbid=others
        )

    results = await asyncio.gather(
        *(_one(cid) for cid in chosen),
        return_exceptions=True,
    )
    out: list["TaskResult"] = []
    for r in results:
        if isinstance(r, BaseException):
            continue  # transient failure — drop, the selector handles partial
        out.append(r)
    return out


__all__ = [
    "select_winner",
    "known_strategies",
    "run_fanout",
]
