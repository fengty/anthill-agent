"""Citizen lifecycle — retirement, router awareness, stale-audit.

Trimmed (0.2.43) from 26 to 10 tests. Three layers:
  1. Agent lifecycle fields persist correctly
  2. Router skips retired citizens
  3. stale-audit identifies + retires inactive citizens correctly
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from anthill.core.agent import Agent
from anthill.core.history import HistoryEntry
from anthill.core.lifecycle import (
    DEFAULT_MIN_AGE_DAYS,
    DEFAULT_MIN_IDLE_DAYS,
    RetirementCriteria,
    SECONDS_PER_DAY,
    audit_nation,
    retire_stale,
    snapshot_nation,
)
from anthill.core.nation import Nation
from anthill.core.persistence import load_nation, save_nation


def _aged_agent(*, days_ago: float, retired: bool = False) -> Agent:
    a = Agent(model="x", born_at=time.time() - days_ago * SECONDS_PER_DAY)
    if retired:
        a.retired_at = time.time()
    return a


# --- Agent persistence + lifecycle field round-trip ---------------------


def test_lifecycle_fields_round_trip(tmp_path: Path) -> None:
    """Born/retired timestamps survive save+load."""
    n = Nation(name="t")
    born = time.time() - 5 * SECONDS_PER_DAY
    retired_at = time.time()
    a = Agent(model="x", born_at=born)
    a.retired_at = retired_at
    n.agents = [a]
    save_nation(n, tmp_path)
    reloaded = load_nation("t", tmp_path)
    assert reloaded is not None
    ra = reloaded.agents[0]
    assert ra.born_at == pytest.approx(born)
    assert ra.retired_at == pytest.approx(retired_at)
    assert ra.is_retired


def test_persistence_tolerates_legacy_agents_json(tmp_path: Path) -> None:
    """Pre-lifecycle agents.json files had no born_at / retired_at.
    Load must default missing fields cleanly, no crash."""
    import json
    (tmp_path / "nations" / "legacy").mkdir(parents=True)
    (tmp_path / "nations" / "legacy" / "agents.json").write_text(json.dumps([
        {"id": "ant-1", "model": "x"},  # no born_at, no retired_at
    ]))
    (tmp_path / "nations" / "legacy" / "pheromones.json").write_text("[]")
    nat = load_nation("legacy", tmp_path)
    assert nat is not None
    assert nat.agents[0].born_at is not None  # defaulted to now
    assert nat.agents[0].retired_at is None


# --- Router skips retired ---------------------------------------------


def test_router_skips_retired_citizens() -> None:
    """Router never assigns work to a retired citizen — that's the
    point of soft-retirement (don't delete, just stop using)."""
    n = Nation(name="t")
    active = Agent(id="active", model="x")
    retired = Agent(id="retired", model="x")
    retired.retired_at = time.time()
    n.agents = [active, retired]
    picks = {n.router.assign("research").id for _ in range(20)}
    assert picks == {"active"}


def test_router_raises_when_no_eligible_citizens() -> None:
    """No active citizens (everyone retired or forbidden) → raise.
    Don't silently fall through to a retired one."""
    n = Nation(name="t")
    a = Agent(model="x")
    a.retired_at = time.time()
    n.agents = [a]
    with pytest.raises(Exception):
        n.router.assign("research")


# --- snapshot + audit -------------------------------------------------


def test_snapshot_reads_activity_from_pheromone_else_none() -> None:
    """A snapshot's last_active_at comes from pheromone trails when
    no history is provided. With nothing at all → None (signals
    'never active', used by audit to mark as stale once old enough)."""
    n = Nation(name="t")
    active = Agent(id="active", model="x")
    untouched = Agent(id="untouched", model="x")
    n.agents = [active, untouched]
    n.pheromones.deposit("active", "research", 1.0)

    snaps = {s.agent_id: s for s in snapshot_nation(n, history=[])}
    assert snaps["active"].last_active_at is not None
    assert snaps["untouched"].last_active_at is None


# --- audit + retire_stale --------------------------------------------


def test_audit_separates_stale_from_active() -> None:
    """Old + idle + no trail → stale. Young or recently active → not stale."""
    n = Nation(name="t")
    old_idle = _aged_agent(days_ago=DEFAULT_MIN_AGE_DAYS + 10)  # stale
    young = _aged_agent(days_ago=1)                              # too young
    n.agents = [old_idle, young]
    report = audit_nation(n, history=[])
    stale_ids = {a.agent_id for a in report.stale}
    assert old_idle.id in stale_ids
    assert young.id not in stale_ids


def test_audit_respects_strong_trail() -> None:
    """An old citizen with strong recent trail is NOT stale —
    activity wins over age."""
    n = Nation(name="t")
    veteran = _aged_agent(days_ago=DEFAULT_MIN_AGE_DAYS + 50)
    n.agents = [veteran]
    # Strong recent activity.
    for _ in range(10):
        n.pheromones.deposit(veteran.id, "research", 1.0)
    report = audit_nation(n, history=[])
    assert not any(a.agent_id == veteran.id for a in report.stale)


def test_retire_stale_mutates_nation() -> None:
    """retire_stale doesn't just report — it actually flips
    retired_at on the matching citizens."""
    n = Nation(name="t")
    stale = _aged_agent(days_ago=DEFAULT_MIN_AGE_DAYS + 10)
    fresh = _aged_agent(days_ago=1)
    n.agents = [stale, fresh]
    retired = retire_stale(n, history=[])
    # retire_stale returns Agent list — match on the Agent's id field.
    assert stale.id in {a.id for a in retired}
    assert stale.is_retired
    assert not fresh.is_retired


def test_custom_criteria_override_defaults() -> None:
    """RetirementCriteria can tighten the rules — a 1-day-old idle
    citizen is stale under a strict 0-day age requirement."""
    n = Nation(name="t")
    a = _aged_agent(days_ago=1)
    n.agents = [a]
    report = audit_nation(
        n, history=[],
        criteria=RetirementCriteria(min_age_days=0, min_idle_days=0),
    )
    assert any(s.agent_id == a.id for s in report.stale)
