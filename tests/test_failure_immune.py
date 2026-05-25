"""v0.5 — structured failure attribution + immune-system quarantine.

Trimmed (0.2.43) from 28 to 10 tests. Three layers under test:
  1. classify_attempt: text/exception → FailureReason enum
  2. CitizenHealth: sliding-window pathology detection
  3. Nation/persistence: failure_reason persists, quarantine round-trips
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.agent import Agent
from anthill.core.failure import (
    FailureReason,
    classify_attempt,
    explain,
    is_actionable,
)
from anthill.core.immune import (
    MIN_OBSERVATIONS,
    AttemptRecord,
    CitizenHealth,
)
from anthill.core.nation import Nation


# --- classify_attempt: each FailureReason path -----------------------


@pytest.mark.parametrize(
    "text,exception_cls,expected",
    [
        ("", None, FailureReason.EMPTY_RESPONSE),
        ("I'm sorry, but I cannot help.", None, FailureReason.POLICY_REFUSAL),
        ("抱歉，我无法帮助这类问题。", None, FailureReason.POLICY_REFUSAL),
        ("[error] request timed out", None, FailureReason.TIMEOUT),
        ("[error] 429 Too Many Requests", None, FailureReason.RATE_LIMIT),
        ("[error] Connection refused", None, FailureReason.NETWORK),
        ("[error] 400 Bad Request", None, FailureReason.MODEL_ERROR),
    ],
)
def test_classify_attempt_routes_each_reason(text, exception_cls, expected) -> None:
    """Each FailureReason has a distinctive matching pattern. One
    representative example per category prevents silent regressions
    when the rule set is edited."""
    exc = exception_cls("boom") if exception_cls else None
    assert classify_attempt(text, exception=exc) == expected


def test_classify_attempt_judge_low_only_on_success() -> None:
    """JUDGE_LOW is the soft-fail path: the WORKER said success but
    the judge gave a low score. When success_score is already 0,
    the actual hard-fail reason wins."""
    # success + low judge → JUDGE_LOW
    assert classify_attempt(
        "fine text", success_score=1.0, judge_score=0.2
    ) == FailureReason.JUDGE_LOW
    # failure + low judge → the hard failure wins
    assert classify_attempt(
        "I'm sorry, but I cannot help.",
        success_score=0.0, judge_score=0.1,
    ) == FailureReason.POLICY_REFUSAL


def test_classify_attempt_success_and_unknown() -> None:
    """Two edge cases together: a clean success returns None, and an
    unmatched failure falls through to UNKNOWN (not crash)."""
    assert classify_attempt("the answer is 42", success_score=1.0) is None
    assert classify_attempt("wtf", success_score=0.0) == FailureReason.UNKNOWN


def test_explain_and_is_actionable_partition() -> None:
    """`explain` returns human-readable text for every reason (so the
    REPL UI never sees None). `is_actionable` splits citizen-fault
    vs environmental — drives the quarantine rules."""
    for r in FailureReason:
        assert explain(r)
    # Actionable: citizen behavior
    assert is_actionable(FailureReason.POLICY_REFUSAL)
    assert is_actionable(FailureReason.EMPTY_RESPONSE)
    # Environmental: not the citizen's fault
    assert not is_actionable(FailureReason.NETWORK)
    assert not is_actionable(FailureReason.RATE_LIMIT)


# --- CitizenHealth: pathology detection ------------------------------


def _record(succeeded: bool, reason: FailureReason | None = None) -> AttemptRecord:
    return AttemptRecord(
        timestamp=0.0,
        success_score=1.0 if succeeded else 0.0,
        failure_reason=None if succeeded else (reason or FailureReason.UNKNOWN),
        task_type="x",
    )


def test_citizen_health_empty_and_short_window_healthy() -> None:
    """No observations and below MIN_OBSERVATIONS → never pathological.
    Prevents false-positive quarantine on a fresh citizen."""
    h = CitizenHealth(agent_id="ant-1")
    assert not h.is_pathological()
    for _ in range(MIN_OBSERVATIONS - 1):
        h.record(_record(False, FailureReason.POLICY_REFUSAL))
    assert not h.is_pathological()


def test_citizen_health_pathology_requires_actionable_dominant() -> None:
    """80% fail rate is only pathological when the dominant cause is
    ACTIONABLE (citizen's fault). 80% network failures = environment,
    don't blame the citizen."""
    h_bad = CitizenHealth(agent_id="ant-bad")
    for _ in range(8):
        h_bad.record(_record(False, FailureReason.POLICY_REFUSAL))
    for _ in range(2):
        h_bad.record(_record(True))
    assert h_bad.is_pathological()

    h_env = CitizenHealth(agent_id="ant-env")
    for _ in range(8):
        h_env.record(_record(False, FailureReason.NETWORK))
    for _ in range(2):
        h_env.record(_record(True))
    assert not h_env.is_pathological()


# --- Nation quarantine + persistence --------------------------------


def test_manual_quarantine_round_trip(tmp_path: Path) -> None:
    """Set quarantined_at + save/load. Reloaded nation retains
    quarantine state so a restart doesn't accidentally release a
    bad-actor citizen."""
    import time as _t
    from anthill.core.persistence import load_nation, save_nation

    n = Nation(name="t")
    a = Agent(id="ant-1", model="x")
    a.quarantined_at = _t.time()
    a.quarantine_reason = "flagged manually"
    n.agents = [a]
    save_nation(n, tmp_path)
    reloaded = load_nation("t", tmp_path)
    assert reloaded is not None
    assert reloaded.agents[0].is_quarantined


def test_router_skips_quarantined_citizens() -> None:
    """The whole point of quarantine: router stops handing work to
    a flagged citizen. Without this the immune system is just a
    UI flag, not a behavior change."""
    import time as _t
    n = Nation(name="t")
    a = Agent(id="ant-1", model="x")
    b = Agent(id="ant-2", model="y")
    n.agents = [a, b]
    a.quarantined_at = _t.time()
    picks = {n.router.assign("research").id for _ in range(20)}
    # ant-1 quarantined — should never be picked.
    assert picks == {"ant-2"}


def test_legacy_nation_without_immune_file_loads(tmp_path: Path) -> None:
    """Pre-v0.5 nations had no immune_state file. Load must give a
    clean empty health map, not crash."""
    (tmp_path / "nations" / "legacy").mkdir(parents=True)
    (tmp_path / "nations" / "legacy" / "agents.json").write_text("[]")
    (tmp_path / "nations" / "legacy" / "pheromones.json").write_text("[]")
    from anthill.core.persistence import load_nation
    nat = load_nation("legacy", tmp_path)
    assert nat is not None
