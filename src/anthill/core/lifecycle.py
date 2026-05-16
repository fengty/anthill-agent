"""Lifecycle — when should a citizen retire?

Until v0.3 every agent was immortal. Spawn it once, it lives forever,
shows up in every roster, gets counted in every diversity metric even
when it has been dormant for months and has no pheromone strength on
anything. That worked for the first 50 asks of a brand new nation; it
fails the moment a long-running nation has accumulated dozens of
citizens, half of them dead weight.

Retirement is the soft-delete:
- the agent stays in nation.agents (history and pheromones still
  resolve its id), but the router stops handing it new tasks.
- it's reversible (`anthill citizen unretire`) — the user might have
  retired the wrong one.
- it's auditable (`anthill citizen audit`) — the criteria are
  deterministic, the user can see who is on the chopping block before
  anything actually changes.

The criteria here intentionally avoid LLM judgment. "Has this agent
been useful?" should be a count, not a vibe. Two signals:
  - idle_days: how long since the citizen last attempted any task
  - max_strength: the highest pheromone trail strength they hold

A citizen is stale when both signals say "nothing is going on":
  idle_days ≥ min_idle_days AND max_strength ≤ max_dead_strength

The thresholds are tunable. Defaults (30 days idle, 0.05 max strength)
are chosen so a citizen has to be both forgotten by the world and
forgotten by the pheromone map. Anything less risks retiring a
recently-spawned specialist that just hasn't been needed yet this
week.

Bootstrap citizens (born within `min_age_days` of now) are never
candidates regardless of the other signals. A nation founded today
shouldn't lose its starter agents at the first audit just because
they haven't accumulated trails yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anthill.core.agent import Agent
    from anthill.core.history import HistoryEntry
    from anthill.core.nation import Nation
    from anthill.core.pheromone import PheromoneTrail


# Defaults chosen to be conservative — false retirements are worse
# than false survivors. A user who wants more aggressive churn can
# pass tighter thresholds.
DEFAULT_MIN_IDLE_DAYS = 30.0
DEFAULT_MAX_DEAD_STRENGTH = 0.05
DEFAULT_MIN_AGE_DAYS = 7.0

SECONDS_PER_DAY = 86_400.0


@dataclass
class CitizenSnapshot:
    """A point-in-time read of one citizen's lifecycle signals.

    Surfaced by `anthill citizen list` and `anthill citizen audit`. The
    `would_retire` flag carries the audit's verdict against a given
    RetirementCriteria so the user can review without committing.
    """

    agent_id: str
    model: str
    born_at: float
    retired_at: float | None
    last_active_at: float | None  # None when the citizen has never run a task
    max_strength: float
    task_attempts: int

    @property
    def is_retired(self) -> bool:
        return self.retired_at is not None

    @property
    def age_days(self) -> float:
        return max(0.0, (time.time() - self.born_at) / SECONDS_PER_DAY)

    @property
    def idle_days(self) -> float | None:
        """Days since last activity, or None for a never-active citizen."""
        if self.last_active_at is None:
            return None
        return max(0.0, (time.time() - self.last_active_at) / SECONDS_PER_DAY)

    def status_label(self) -> str:
        if self.is_retired:
            return "retired"
        if self.last_active_at is None:
            return "untested"
        if self.idle_days is not None and self.idle_days > 7:
            return "quiet"
        return "active"


@dataclass
class RetirementCriteria:
    """Thresholds for `is_stale`. All knobs explicit on purpose."""

    min_idle_days: float = DEFAULT_MIN_IDLE_DAYS
    max_dead_strength: float = DEFAULT_MAX_DEAD_STRENGTH
    min_age_days: float = DEFAULT_MIN_AGE_DAYS

    def is_stale(self, snapshot: CitizenSnapshot) -> bool:
        """Does this citizen meet every condition for retirement?

        Returns False if the citizen is already retired (don't double-act),
        too young (bootstrap protection), or has any pheromone strength
        above the dead-trail threshold. A citizen that has never run a
        task counts as idle for any number of days — `idle_days is None`
        is treated as "infinitely idle" only if min_age_days has been
        cleared.
        """
        if snapshot.is_retired:
            return False
        if snapshot.age_days < self.min_age_days:
            return False
        if snapshot.max_strength > self.max_dead_strength:
            return False
        if snapshot.idle_days is None:
            # Never-active citizen old enough to count: stale.
            return True
        return snapshot.idle_days >= self.min_idle_days


@dataclass
class AuditReport:
    """The result of `audit_nation`. Either render or act on it."""

    citizens: list[CitizenSnapshot]
    stale: list[CitizenSnapshot]   # the subset that would be retired
    criteria: RetirementCriteria

    @property
    def active_count(self) -> int:
        return sum(1 for c in self.citizens if not c.is_retired)

    @property
    def retired_count(self) -> int:
        return sum(1 for c in self.citizens if c.is_retired)


# --- helpers --------------------------------------------------------------


def _last_active_at_by_agent(history: list["HistoryEntry"]) -> dict[str, float]:
    """Walk history once; for each citizen, capture their most recent activity.

    history.HistoryEntry stores outcomes shape-only (task_type, status,
    attempts count) — the per-attempt agent_id is not retained there.
    We approximate by using the entry timestamp for every citizen that
    contributed to the entry; for the inactivity check we only need
    "did this agent do anything recently?", not which subtask.

    When history doesn't carry per-citizen attribution at all, the
    pheromone trail's last_updated is the secondary signal that
    `_max_strength_by_agent` plumbs through separately.
    """
    last: dict[str, float] = {}
    for entry in history:
        ts = float(entry.timestamp)
        # Some HistoryEntry outcome dicts carry an agent_id field on the
        # final attempt; honor it when present. Otherwise we can't credit
        # specific citizens — that case falls back to pheromone signal.
        for outcome in entry.outcomes:
            agent_id = outcome.get("agent_id") if isinstance(outcome, dict) else None
            if not agent_id:
                continue
            if last.get(agent_id, 0.0) < ts:
                last[agent_id] = ts
    return last


def _max_strength_by_agent(pheromones: "PheromoneTrail") -> dict[str, float]:
    """For each citizen, their strongest trail across all task_types.

    A specialist citizen typically holds one or two strong trails; a
    dormant citizen's strengths have all decayed toward zero. We pick
    the max because retirement should only fire when EVERY trail has
    died, not when the average has — averaging would penalize a
    rifle-precise specialist that does one thing very well.
    """
    out: dict[str, float] = {}
    for trail in pheromones.trails():
        prev = out.get(trail.agent_id, 0.0)
        if trail.strength > prev:
            out[trail.agent_id] = trail.strength
    return out


def _last_pheromone_update_by_agent(pheromones: "PheromoneTrail") -> dict[str, float]:
    """Secondary inactivity signal when history lacks per-attempt agent_id."""
    out: dict[str, float] = {}
    for trail in pheromones.trails():
        prev = out.get(trail.agent_id, 0.0)
        if trail.last_updated > prev:
            out[trail.agent_id] = trail.last_updated
    return out


def _attempt_count_by_agent(pheromones: "PheromoneTrail") -> dict[str, int]:
    """Count of distinct task_types per agent — a crude 'tried this many things'."""
    out: dict[str, int] = {}
    for trail in pheromones.trails():
        out[trail.agent_id] = out.get(trail.agent_id, 0) + 1
    return out


# --- public API -----------------------------------------------------------


def snapshot_nation(
    nation: "Nation",
    history: list["HistoryEntry"],
) -> list[CitizenSnapshot]:
    """One CitizenSnapshot per agent, regardless of retired status."""
    last_active_history = _last_active_at_by_agent(history)
    last_active_pher = _last_pheromone_update_by_agent(nation.pheromones)
    max_strength = _max_strength_by_agent(nation.pheromones)
    attempts = _attempt_count_by_agent(nation.pheromones)

    snaps: list[CitizenSnapshot] = []
    for agent in nation.agents:
        # Prefer history's per-attempt timestamp (more accurate); fall back
        # to the pheromone trail's last_updated when history doesn't say.
        last_active = last_active_history.get(agent.id)
        if last_active is None:
            last_active = last_active_pher.get(agent.id)
        snaps.append(
            CitizenSnapshot(
                agent_id=agent.id,
                model=agent.model,
                born_at=agent.born_at,
                retired_at=agent.retired_at,
                last_active_at=last_active,
                max_strength=max_strength.get(agent.id, 0.0),
                task_attempts=attempts.get(agent.id, 0),
            )
        )
    return snaps


def audit_nation(
    nation: "Nation",
    history: list["HistoryEntry"],
    criteria: RetirementCriteria | None = None,
) -> AuditReport:
    """Compute the retirement audit without mutating anything.

    The caller decides what to do with `report.stale` — print it,
    confirm with the user, or call `retire_stale` to act.
    """
    crit = criteria or RetirementCriteria()
    snaps = snapshot_nation(nation, history)
    stale = [s for s in snaps if crit.is_stale(s)]
    return AuditReport(citizens=snaps, stale=stale, criteria=crit)


def retire_stale(
    nation: "Nation",
    history: list["HistoryEntry"],
    criteria: RetirementCriteria | None = None,
) -> list["Agent"]:
    """Apply the audit's verdict. Returns the citizens actually retired."""
    report = audit_nation(nation, history, criteria)
    retired: list["Agent"] = []
    for snap in report.stale:
        agent = nation.retire(snap.agent_id)
        if agent is not None:
            retired.append(agent)
    return retired


__all__ = [
    "CitizenSnapshot",
    "RetirementCriteria",
    "AuditReport",
    "snapshot_nation",
    "audit_nation",
    "retire_stale",
    "DEFAULT_MIN_IDLE_DAYS",
    "DEFAULT_MAX_DEAD_STRENGTH",
    "DEFAULT_MIN_AGE_DAYS",
]
