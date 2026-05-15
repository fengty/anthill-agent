"""National strength — a quantified picture of how capable the nation has become.

Six dimensions, each one a number the king can watch climb:

    vocabulary       distinct task_types the nation has handled
    specialists      citizens with at least one strong trail
    success_rate     ok-ratio over the entire history
    max_chain        longest plan (number of subtasks) executed successfully
    feedback_score   net rating volume (ups minus downs)
    diversity        how spread the work is across citizens (entropy-based)

Together they form a national "power level." This is more useful than
any single metric because growing a nation has multiple axes — a nation
with high specialists but low max_chain is broad but shallow; reverse
that and you have deep but narrow. The user can see both.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from anthill.core.feedback import Exemplar
from anthill.core.history import HistoryEntry
from anthill.core.nation import Nation


@dataclass
class PowerReport:
    vocabulary: int
    specialists: int
    success_rate: float  # [0, 1]
    max_chain: int
    feedback_score: int
    diversity: float  # [0, 1]
    total_tasks: int
    total_asks: int

    @property
    def overall(self) -> float:
        """A single 0-100 score combining the dimensions.

        Weighted sum, capped at 100. Not a precise statistic; a barometer.
        The king should watch the constituent dimensions for honest insight.
        """
        score = 0.0
        score += min(self.vocabulary * 5, 25)        # up to 25 for variety
        score += min(self.specialists * 5, 20)        # up to 20 for breadth
        score += self.success_rate * 20               # up to 20 for reliability
        score += min(self.max_chain * 4, 15)          # up to 15 for depth
        score += min(max(self.feedback_score, 0), 10)  # up to 10 for approval
        score += self.diversity * 10                  # up to 10 for healthy spread
        return min(score, 100.0)


def compute_power(
    nation: Nation,
    history: list[HistoryEntry],
    exemplars: list[Exemplar],
    *,
    strong_trail_threshold: float = 2.0,
) -> PowerReport:
    """Synthesize the metrics from current state."""
    vocabulary = len(nation.culture.task_catalog)

    # A specialist is a citizen with at least one trail above the threshold.
    trail_agents: dict[str, float] = {}
    for trail in nation.pheromones.trails():
        if trail.strength >= strong_trail_threshold:
            trail_agents[trail.agent_id] = max(
                trail_agents.get(trail.agent_id, 0.0), trail.strength
            )
    specialists = len(trail_agents)

    # Success rate from history outcomes.
    total_tasks = 0
    successful_tasks = 0
    max_chain = 0
    for entry in history:
        chain_ok = all(o["status"] == "ok" for o in entry.outcomes)
        if chain_ok:
            max_chain = max(max_chain, len(entry.outcomes))
        for o in entry.outcomes:
            total_tasks += 1
            if o["status"] == "ok":
                successful_tasks += 1
    success_rate = (successful_tasks / total_tasks) if total_tasks else 0.0

    feedback_score = sum(1 if e.rating == "up" else -1 for e in exemplars)

    # Diversity: Shannon entropy of work distribution across citizens,
    # normalised to [0, 1]. Higher = work is spread; lower = one citizen
    # is doing everything.
    citizen_counts: dict[str, int] = {}
    for trail in nation.pheromones.trails():
        citizen_counts[trail.agent_id] = citizen_counts.get(trail.agent_id, 0) + 1
    diversity = _normalized_entropy(citizen_counts.values())

    return PowerReport(
        vocabulary=vocabulary,
        specialists=specialists,
        success_rate=success_rate,
        max_chain=max_chain,
        feedback_score=feedback_score,
        diversity=diversity,
        total_tasks=total_tasks,
        total_asks=len(history),
    )


def _normalized_entropy(counts) -> float:
    counts = list(counts)
    if len(counts) < 2:
        return 0.0
    total = sum(counts)
    if total == 0:
        return 0.0
    probs = [c / total for c in counts]
    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    max_entropy = math.log2(len(counts))
    return entropy / max_entropy if max_entropy > 0 else 0.0
