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
class AgeMilestone:
    """One of the four ages a nation passes through as it grows."""

    name: str
    description: str
    completed: bool
    progress: float  # [0, 1] — partial credit for ages still in progress


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


def compute_ages(
    nation: Nation,
    history: list[HistoryEntry],
    exemplars: list[Exemplar],
) -> list[AgeMilestone]:
    """The four ages a nation passes through.

    Founding         once any citizen has been spawned
    Specialization   once at least one citizen has a strong (>=2.0) trail
    Culture          once a house style is set OR 5+ task types in catalog
    Statecraft       once a multi-step (>=3) plan has run end-to-end successfully

    Each age has fractional progress so the user can see how far they
    are from the next one — not just binary done/not-done.
    """
    # Founding
    citizens = len(nation.agents)
    founding_progress = min(citizens / 3.0, 1.0)
    founding_done = citizens >= 1

    # Specialization
    strong_trails = sum(1 for t in nation.pheromones.trails() if t.strength >= 2.0)
    spec_progress = min(strong_trails / 3.0, 1.0)
    spec_done = strong_trails >= 1

    # Culture
    vocab = len(nation.culture.task_catalog)
    has_style = bool(nation.culture.house_style.strip())
    culture_progress = min((vocab / 5.0), 1.0)
    if has_style:
        culture_progress = max(culture_progress, 0.5)
    culture_done = has_style or vocab >= 5

    # Statecraft
    longest_successful_chain = 0
    for entry in history:
        if all(o["status"] == "ok" for o in entry.outcomes):
            longest_successful_chain = max(longest_successful_chain, len(entry.outcomes))
    statecraft_progress = min(longest_successful_chain / 3.0, 1.0)
    statecraft_done = longest_successful_chain >= 3

    return [
        AgeMilestone(
            name="Founding",
            description=f"{citizens} citizen(s) spawned",
            completed=founding_done,
            progress=founding_progress,
        ),
        AgeMilestone(
            name="Specialization",
            description=f"{strong_trails} strong trail(s) (need 1+)",
            completed=spec_done,
            progress=spec_progress,
        ),
        AgeMilestone(
            name="Culture",
            description=(
                f"vocabulary {vocab}, style {'set' if has_style else 'unset'}"
            ),
            completed=culture_done,
            progress=culture_progress,
        ),
        AgeMilestone(
            name="Statecraft",
            description=f"longest successful chain: {longest_successful_chain} step(s) (need 3+)",
            completed=statecraft_done,
            progress=statecraft_progress,
        ),
    ]


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
