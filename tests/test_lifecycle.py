"""Citizen lifecycle tests — soft retirement, router awareness, stale-audit.

Three layers:
1. Agent dataclass carries the new fields and reads/writes them through
   persistence (no data loss on the existing snapshot path).
2. Router filters retired citizens out of assignment.
3. lifecycle.audit_nation correctly classifies snapshots against the
   RetirementCriteria thresholds, with bootstrap protection and the
   "never-active = stale-once-old-enough" semantics intact.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from anthill.core.agent import Agent
from anthill.core.history import HistoryEntry
from anthill.core.lifecycle import (
    DEFAULT_MAX_DEAD_STRENGTH,
    DEFAULT_MIN_AGE_DAYS,
    DEFAULT_MIN_IDLE_DAYS,
    RetirementCriteria,
    SECONDS_PER_DAY,
    audit_nation,
    retire_stale,
    snapshot_nation,
)
from anthill.core.nation import Nation
from anthill.core.pheromone import PheromoneTrail
from anthill.core.persistence import load_nation, save_nation
from anthill.core.router import Router


# --- helpers --------------------------------------------------------------


def _aged_agent(*, days_ago: float, retired: bool = False) -> Agent:
    """Build an agent that pretends it was born N days ago."""
    a = Agent(model="deepseek-chat", born_at=time.time() - days_ago * SECONDS_PER_DAY)
    if retired:
        a.retired_at = time.time()
    return a


# --- Agent dataclass + persistence ----------------------------------------


def test_new_agent_is_not_retired() -> None:
    a = Agent(model="x")
    assert a.is_retired is False
    assert a.retired_at is None
    assert a.born_at > 0


def test_persistence_round_trip_preserves_lifecycle_fields(tmp_path: Path) -> None:
    n = Nation(name="testnat")
    a1 = _aged_agent(days_ago=45)
    a2 = _aged_agent(days_ago=10, retired=True)
    n.agents = [a1, a2]
    save_nation(n, tmp_path)

    reloaded = load_nation("testnat", tmp_path)
    assert reloaded is not None
    by_id = {a.id: a for a in reloaded.agents}
    assert by_id[a1.id].is_retired is False
    assert by_id[a1.id].born_at == pytest.approx(a1.born_at)
    assert by_id[a2.id].is_retired is True
    assert by_id[a2.id].retired_at is not None


def test_persistence_tolerates_legacy_agents_json(tmp_path: Path) -> None:
    """Older agents.json files lack born_at / retired_at — load shouldn't crash."""
    import json
    (tmp_path / "nations" / "legacy").mkdir(parents=True)
    legacy = [
        {
            "id": "ant-legacy01",
            "model": "deepseek-chat",
            "persona": None,
            "private_memory": {},
        }
    ]
    (tmp_path / "nations" / "legacy" / "agents.json").write_text(json.dumps(legacy))
    (tmp_path / "nations" / "legacy" / "pheromones.json").write_text("[]")

    nat = load_nation("legacy", tmp_path)
    assert nat is not None
    assert len(nat.agents) == 1
    a = nat.agents[0]
    assert a.id == "ant-legacy01"
    assert a.is_retired is False
    # born_at should be filled by the default-factory (≈ now), not zero
    assert a.born_at > 0


# --- Nation lifecycle methods ---------------------------------------------


def test_retire_marks_agent_and_returns_it() -> None:
    n = Nation(name="t")
    a = _aged_agent(days_ago=1)
    n.agents = [a]
    out = n.retire(a.id)
    assert out is a
    assert a.is_retired is True
    assert a.retired_at is not None


def test_retire_unknown_returns_none() -> None:
    n = Nation(name="t")
    assert n.retire("nobody") is None


def test_retire_already_retired_is_noop() -> None:
    """Idempotency: re-retiring shouldn't reset the timestamp or signal change."""
    n = Nation(name="t")
    a = _aged_agent(days_ago=1, retired=True)
    n.agents = [a]
    first_retired_at = a.retired_at
    assert n.retire(a.id) is None
    assert a.retired_at == first_retired_at  # unchanged


def test_unretire_restores_active_status() -> None:
    n = Nation(name="t")
    a = _aged_agent(days_ago=1, retired=True)
    n.agents = [a]
    assert n.unretire(a.id) is a
    assert a.is_retired is False
    assert a.retired_at is None


def test_unretire_on_active_returns_none() -> None:
    n = Nation(name="t")
    a = _aged_agent(days_ago=1)
    n.agents = [a]
    assert n.unretire(a.id) is None


def test_find_agent_supports_prefix() -> None:
    n = Nation(name="t")
    a = _aged_agent(days_ago=1)
    n.agents = [a]
    short = a.id[:5]
    assert n.find_agent(short) is a


def test_alive_agents_excludes_retired() -> None:
    n = Nation(name="t")
    a1 = _aged_agent(days_ago=1)
    a2 = _aged_agent(days_ago=1, retired=True)
    n.agents = [a1, a2]
    assert n.alive_agents() == [a1]


# --- Router awareness -----------------------------------------------------


def test_router_skips_retired_agents() -> None:
    pher = PheromoneTrail()
    a1 = _aged_agent(days_ago=1)
    a2 = _aged_agent(days_ago=1, retired=True)
    router = Router(pher, [a1, a2])
    # 100 picks: never pick the retired one.
    picks = {router.assign("any").id for _ in range(100)}
    assert a1.id in picks
    assert a2.id not in picks


def test_router_raises_when_all_active_are_forbidden() -> None:
    pher = PheromoneTrail()
    a1 = _aged_agent(days_ago=1)
    a2 = _aged_agent(days_ago=1, retired=True)
    router = Router(pher, [a1, a2])
    with pytest.raises(RuntimeError, match="forbidden, retired"):
        router.assign("any", forbid={a1.id})


def test_router_raises_when_every_active_is_retired() -> None:
    pher = PheromoneTrail()
    a1 = _aged_agent(days_ago=1, retired=True)
    a2 = _aged_agent(days_ago=1, retired=True)
    router = Router(pher, [a1, a2])
    with pytest.raises(RuntimeError):
        router.assign("any")


# --- Lifecycle snapshot + audit -------------------------------------------


def _entry(ts: float, agent_id: str) -> HistoryEntry:
    """Synthesize a history entry crediting `agent_id` at time `ts`."""
    return HistoryEntry(
        id="abc12345",
        timestamp=ts,
        request="r",
        plan=[],
        outcomes=[
            {
                "task_type": "x",
                "status": "ok",
                "attempts": 1,
                "final_output": "ok",
                "skip_reason": None,
                "agent_id": agent_id,
            }
        ],
    )


def test_snapshot_marks_never_active_with_none_last_active() -> None:
    n = Nation(name="t")
    a = _aged_agent(days_ago=1)
    n.agents = [a]
    snaps = snapshot_nation(n, history=[])
    assert len(snaps) == 1
    assert snaps[0].last_active_at is None
    assert snaps[0].idle_days is None


def test_snapshot_uses_history_for_recent_activity() -> None:
    n = Nation(name="t")
    a = _aged_agent(days_ago=10)
    n.agents = [a]
    yesterday = time.time() - 86_400
    snaps = snapshot_nation(n, history=[_entry(yesterday, a.id)])
    assert snaps[0].last_active_at == pytest.approx(yesterday)
    assert snaps[0].idle_days is not None
    assert snaps[0].idle_days < 2.0


def test_snapshot_falls_back_to_pheromone_when_history_silent() -> None:
    """No-agent-id history shouldn't shadow the pheromone trail's last_updated.

    Note: pheromone strength decays on every read, so we deposit a fresh
    trail (last_updated ~= now) — the test is about the fallback path, not
    about the absolute strength surviving across time.
    """
    n = Nation(name="t")
    a = _aged_agent(days_ago=10)
    n.agents = [a]
    n.pheromones.deposit(agent_id=a.id, task_type="x", success_score=1.0)
    snaps = snapshot_nation(n, history=[])
    assert snaps[0].last_active_at is not None
    assert snaps[0].max_strength > 0.5  # fresh deposit, undecayed


# --- RetirementCriteria.is_stale ------------------------------------------


def _snap(**overrides):  # noqa: ANN001, ANN201
    from anthill.core.lifecycle import CitizenSnapshot
    defaults = dict(
        agent_id="ant-x",
        model="m",
        born_at=time.time() - 60 * SECONDS_PER_DAY,
        retired_at=None,
        last_active_at=None,
        max_strength=0.0,
        task_attempts=0,
    )
    defaults.update(overrides)
    return CitizenSnapshot(**defaults)


def test_default_constants_are_conservative() -> None:
    """Defaults should be safe — false retirements are worse than false survivors."""
    assert DEFAULT_MIN_IDLE_DAYS >= 14
    assert DEFAULT_MAX_DEAD_STRENGTH <= 0.1
    assert DEFAULT_MIN_AGE_DAYS >= 1


def test_already_retired_never_stale() -> None:
    crit = RetirementCriteria()
    s = _snap(retired_at=time.time())
    assert crit.is_stale(s) is False


def test_too_young_never_stale() -> None:
    """Bootstrap protection: a fresh nation shouldn't lose starter agents."""
    crit = RetirementCriteria()
    s = _snap(born_at=time.time() - 2 * SECONDS_PER_DAY)  # 2 days old
    assert crit.is_stale(s) is False


def test_strong_trail_never_stale() -> None:
    crit = RetirementCriteria()
    s = _snap(
        last_active_at=time.time() - 100 * SECONDS_PER_DAY,  # very idle
        max_strength=0.5,  # but still strong
    )
    assert crit.is_stale(s) is False


def test_idle_with_dead_trail_is_stale() -> None:
    crit = RetirementCriteria()
    s = _snap(
        last_active_at=time.time() - 45 * SECONDS_PER_DAY,
        max_strength=0.01,
    )
    assert crit.is_stale(s) is True


def test_never_active_old_enough_is_stale() -> None:
    """A citizen that never ran anything counts as infinitely idle once aged."""
    crit = RetirementCriteria()
    s = _snap(last_active_at=None, max_strength=0.0)
    assert crit.is_stale(s) is True


def test_custom_criteria_override_defaults() -> None:
    crit = RetirementCriteria(
        min_idle_days=5, max_dead_strength=0.5, min_age_days=1
    )
    s = _snap(
        last_active_at=time.time() - 7 * SECONDS_PER_DAY,
        max_strength=0.3,
    )
    assert crit.is_stale(s) is True


# --- audit_nation + retire_stale -----------------------------------------


def test_audit_separates_stale_from_active() -> None:
    """Mix of old-and-dead + new-and-active; only the former should be flagged."""
    n = Nation(name="t")
    old_dead = _aged_agent(days_ago=60)        # old, no activity, no trails
    young_fresh = _aged_agent(days_ago=2)      # bootstrap-protected
    n.agents = [old_dead, young_fresh]

    report = audit_nation(n, history=[])
    assert len(report.citizens) == 2
    assert [s.agent_id for s in report.stale] == [old_dead.id]


def test_retire_stale_actually_mutates_the_nation() -> None:
    """A citizen with a fresh strong trail should survive the audit.

    Decay is real: a trail that was strong 60 days ago is essentially 0
    today. So "keep_me" has to be both old AND recently-active to test
    that the auditor distinguishes them from old-and-dead citizens.
    """
    n = Nation(name="t")
    old_dead = _aged_agent(days_ago=60)
    keep_me = _aged_agent(days_ago=60)
    n.agents = [old_dead, keep_me]
    # Fresh deposit — both fresh activity AND strong trail in one step.
    n.pheromones.deposit(agent_id=keep_me.id, task_type="x", success_score=1.0)

    retired = retire_stale(n, history=[])
    assert [a.id for a in retired] == [old_dead.id]
    assert old_dead.is_retired is True
    assert keep_me.is_retired is False


def test_audit_counts_active_vs_retired() -> None:
    n = Nation(name="t")
    n.agents = [
        _aged_agent(days_ago=10),
        _aged_agent(days_ago=10, retired=True),
        _aged_agent(days_ago=10, retired=True),
    ]
    report = audit_nation(n, history=[])
    assert report.active_count == 1
    assert report.retired_count == 2
