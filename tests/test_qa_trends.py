"""0.2.37 — cross-session trend aggregation.

`/test trends` walks all persisted sessions and surfaces:
  - overall pass rate
  - cases that are reliably passing (regression-safe)
  - cases that are flaky (sometimes pass, sometimes fail)
  - cases that are broken (≥2 runs, 0 passes)
  - cases too new to judge (1 run only)
  - recent failures

Tests build a synthetic session history then verify the
categorization rules.
"""

from __future__ import annotations

import time
from pathlib import Path

from anthill.core.qa import (
    CaseStats,
    TestCase,
    TestResult,
    TestSession,
    aggregate_trends,
    write_session_json,
)


def _session(
    *,
    cases_with_status: list[tuple[str, str]],
    started_at: float,
    requirement: str = "x",
) -> TestSession:
    """Build a session with N (name, status) cases."""
    cases = []
    results = []
    for i, (name, status) in enumerate(cases_with_status, start=1):
        c = TestCase(id=i, name=name, expected=f"expects-{name}")
        cases.append(c)
        results.append(TestResult(
            case=c,
            status=status,
            error=f"{name} failed" if status != "passed" else None,
            duration_seconds=0.5,
        ))
    s = TestSession(
        requirement=requirement,
        cases=cases,
        results=results,
        started_at=started_at,
        ended_at=started_at + 5,
        nation_name="t",
    )
    return s


# --- empty / no sessions ---------------------------------------------


def test_empty_history(tmp_path: Path) -> None:
    trends = aggregate_trends(tmp_path)
    assert trends.total_sessions == 0
    assert trends.total_case_runs == 0
    assert trends.overall_pass_rate == 0.0
    assert trends.by_case == {}


# --- categorization rules --------------------------------------------


def test_categorizes_reliable_flaky_broken_new(tmp_path: Path) -> None:
    """Build 4 sessions covering all four categories."""
    base = time.time() - 1000
    # case "always-pass" passes in all 3 sessions → reliable
    # case "sometimes" passes in 2 of 3 → flaky
    # case "broken" fails in all 3 → broken
    # case "untested" appears once → new
    write_session_json(_session(
        cases_with_status=[
            ("always-pass", "passed"),
            ("sometimes", "passed"),
            ("broken", "failed"),
        ],
        started_at=base + 1,
    ), tmp_path)
    time.sleep(0.01)
    write_session_json(_session(
        cases_with_status=[
            ("always-pass", "passed"),
            ("sometimes", "failed"),
            ("broken", "failed"),
        ],
        started_at=base + 2,
    ), tmp_path)
    time.sleep(0.01)
    write_session_json(_session(
        cases_with_status=[
            ("always-pass", "passed"),
            ("sometimes", "passed"),
            ("broken", "failed"),
            ("untested", "passed"),
        ],
        started_at=base + 3,
    ), tmp_path)

    trends = aggregate_trends(tmp_path)
    assert trends.total_sessions == 3

    by = trends.by_case
    assert by["always-pass"].flakiness == "reliable"
    assert by["always-pass"].passed == 3 and by["always-pass"].runs == 3

    assert by["sometimes"].flakiness == "flaky"
    assert by["sometimes"].passed == 2 and by["sometimes"].runs == 3
    assert by["sometimes"].pass_rate == pytest_approx(2 / 3)

    assert by["broken"].flakiness == "broken"
    assert by["broken"].passed == 0 and by["broken"].runs == 3

    assert by["untested"].flakiness == "new"
    assert by["untested"].runs == 1


def test_lists_partition_by_category(tmp_path: Path) -> None:
    """The convenience properties (.reliable, .flaky, .broken, .fresh)
    return cases matching that category only."""
    base = time.time() - 100
    write_session_json(_session(
        cases_with_status=[("a", "passed"), ("b", "failed"), ("c", "passed")],
        started_at=base + 1,
    ), tmp_path)
    time.sleep(0.01)
    write_session_json(_session(
        cases_with_status=[("a", "passed"), ("b", "passed")],
        started_at=base + 2,
    ), tmp_path)

    trends = aggregate_trends(tmp_path)
    # "a" passed twice → reliable
    # "b" passed-then-failed → flaky
    # "c" passed once → new
    reliable_names = [c.name for c in trends.reliable]
    flaky_names = [c.name for c in trends.flaky]
    fresh_names = [c.name for c in trends.fresh]
    assert "a" in reliable_names
    assert "b" in flaky_names
    assert "c" in fresh_names


# --- overall pass rate -----------------------------------------------


def test_overall_pass_rate_aggregates(tmp_path: Path) -> None:
    """3 + 4 = 7 total runs, 2 + 3 = 5 passed → 5/7."""
    base = time.time() - 100
    write_session_json(_session(
        cases_with_status=[("a", "passed"), ("b", "passed"), ("c", "failed")],
        started_at=base + 1,
    ), tmp_path)
    time.sleep(0.01)
    write_session_json(_session(
        cases_with_status=[
            ("d", "passed"), ("e", "passed"), ("f", "passed"), ("g", "failed"),
        ],
        started_at=base + 2,
    ), tmp_path)

    trends = aggregate_trends(tmp_path)
    assert trends.total_case_runs == 7
    assert trends.overall_pass_rate == pytest_approx(5 / 7)


# --- recent failures ------------------------------------------------


def test_recent_failures_include_session_id_and_case(tmp_path: Path) -> None:
    """Failures show up in trends.recent_failures with their session
    id + case name + error summary."""
    base = time.time() - 100
    write_session_json(_session(
        cases_with_status=[("login broken", "failed")],
        started_at=base + 1,
    ), tmp_path)
    trends = aggregate_trends(tmp_path)
    assert len(trends.recent_failures) == 1
    sid, name, err = trends.recent_failures[0]
    assert name == "login broken"
    assert "failed" in err


def test_recent_failures_capped(tmp_path: Path) -> None:
    """Don't return 1000 failures; cap at 20."""
    base = time.time() - 100
    for i in range(30):
        write_session_json(_session(
            cases_with_status=[(f"case-{i}", "failed")],
            started_at=base + i,
        ), tmp_path)
    trends = aggregate_trends(tmp_path)
    assert len(trends.recent_failures) <= 20


# --- last_status / last_seen tracks newest -------------------------


def test_last_status_reflects_most_recent_run(tmp_path: Path) -> None:
    """A case that USED TO fail but now passes should have
    last_status='passed'."""
    base = time.time() - 100
    write_session_json(_session(
        cases_with_status=[("recovers", "failed")],
        started_at=base + 1,
    ), tmp_path)
    time.sleep(0.01)
    write_session_json(_session(
        cases_with_status=[("recovers", "passed")],
        started_at=base + 100,
    ), tmp_path)
    trends = aggregate_trends(tmp_path)
    case = trends.by_case["recovers"]
    assert case.last_status == "passed"
    assert case.pass_rate == 0.5
    assert case.flakiness == "flaky"  # 1/2 passes


# --- pytest_approx helper (no pytest_approx import to keep dep small)


def pytest_approx(x):
    """Tiny approx helper — pytest's approx is overkill for our 1e-9 needs."""
    class _Approx:
        def __init__(self, v): self.v = v
        def __eq__(self, other): return abs(other - self.v) < 1e-9
        def __repr__(self): return f"~{self.v}"
    return _Approx(x)
