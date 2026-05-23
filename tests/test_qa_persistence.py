"""0.2.36 — TestSession persistence + retest infrastructure.

/test now writes a JSON sibling next to the markdown report so
/retest can rehydrate a past run and re-execute failed cases.

Tests:
  - JSON round-trip preserves every field including fix_attempts
  - Session ID + slug derivation is stable
  - list_sessions sorts newest first, skips corrupt files
  - resolve_session: latest / by exact id / by prefix
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from anthill.core.qa import (
    FixAttempt,
    TestCase,
    TestResult,
    TestSession,
    list_sessions,
    load_session_json,
    reports_dir,
    resolve_session,
    write_session_json,
)


def _make_session(*, nation="t", passed_first=True) -> TestSession:
    case_a = TestCase(id=1, name="login works", expected="dashboard")
    case_b = TestCase(id=2, name="bad pw fails", expected="error")
    result_a = TestResult(
        case=case_a,
        status="passed" if passed_first else "failed",
        narrative="ran login. VERDICT: PASS" if passed_first else "VERDICT: FAIL no dashboard",
        duration_seconds=2.1,
        actions_taken=4,
        evidence=["screenshot-1.png"],
        error=None if passed_first else "no dashboard",
    )
    result_b = TestResult(
        case=case_b, status="failed",
        narrative="VERDICT: FAIL no error element",
        duration_seconds=1.5,
        actions_taken=3,
        error="no error element",
        fix_attempts=[
            FixAttempt(
                attempt=1, fix_status="fixed",
                fix_summary="added .error-msg",
                rerun_status="failed",
                rerun_narrative="VERDICT: FAIL wrong text",
                duration_seconds=3.2,
            ),
        ],
    )
    return TestSession(
        requirement="login requirement here",
        cases=[case_a, case_b],
        results=[result_a, result_b],
        nation_name=nation,
        ended_at=time.time(),
    )


# --- JSON round-trip --------------------------------------------------


def test_json_roundtrip_preserves_session(tmp_path: Path) -> None:
    """Write → read → identical contents (modulo time precision)."""
    session = _make_session()
    path = write_session_json(session, tmp_path)
    assert path.exists()
    assert path.suffix == ".json"

    loaded = load_session_json(path)
    assert loaded.requirement == session.requirement
    assert loaded.nation_name == session.nation_name
    assert len(loaded.cases) == len(session.cases)
    assert len(loaded.results) == len(session.results)

    # Case content preserved.
    assert loaded.cases[0].name == "login works"
    assert loaded.cases[0].expected == "dashboard"

    # Result content preserved including evidence + error.
    rb = loaded.results[1]
    assert rb.status == "failed"
    assert "no error element" in (rb.error or "")
    assert rb.evidence == []  # case_b had none
    assert loaded.results[0].evidence == ["screenshot-1.png"]

    # Fix attempts preserved with all fields.
    assert len(rb.fix_attempts) == 1
    fa = rb.fix_attempts[0]
    assert fa.fix_status == "fixed"
    assert fa.fix_summary == "added .error-msg"
    assert fa.rerun_status == "failed"


def test_json_filename_includes_timestamp_and_slug(tmp_path: Path) -> None:
    """Path is human-readable: YYYYMMDD-HHMMSS-<slug>.json"""
    session = _make_session()
    path = write_session_json(session, tmp_path)
    assert path.name.endswith(".json")
    # First case name "login works" → slug "login-works"
    assert "login-works" in path.stem


def test_json_lives_under_test_reports(tmp_path: Path) -> None:
    """The JSON file lives next to markdown in test_reports/."""
    session = _make_session()
    path = write_session_json(session, tmp_path)
    assert path.parent == reports_dir(tmp_path)


# --- list_sessions ----------------------------------------------------


def test_list_sessions_empty(tmp_path: Path) -> None:
    assert list_sessions(tmp_path) == []


def test_list_sessions_returns_metadata(tmp_path: Path) -> None:
    session = _make_session()
    write_session_json(session, tmp_path)
    metas = list_sessions(tmp_path)
    assert len(metas) == 1
    m = metas[0]
    assert m.total == 2
    assert m.passed == 1
    assert m.failed == 1


def test_list_sessions_newest_first(tmp_path: Path) -> None:
    """Two sessions, second one wins position 0."""
    s1 = _make_session()
    s1.started_at = time.time() - 100  # older
    write_session_json(s1, tmp_path)
    time.sleep(0.01)  # ensure different filename timestamp

    s2 = _make_session()
    write_session_json(s2, tmp_path)

    metas = list_sessions(tmp_path)
    assert len(metas) >= 1
    # Newest-first sort, so metas[0] is s2 (later started_at).
    assert metas[0].started_at >= metas[-1].started_at


def test_list_sessions_skips_corrupt(tmp_path: Path) -> None:
    """A garbage .json doesn't break listing the rest."""
    d = reports_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "20260524-000000-junk.json").write_text("{not valid json")

    session = _make_session()
    write_session_json(session, tmp_path)

    metas = list_sessions(tmp_path)
    # The corrupt one is skipped, the valid one returns.
    assert len(metas) == 1


# --- resolve_session --------------------------------------------------


def test_resolve_latest_when_no_selector(tmp_path: Path) -> None:
    s1 = _make_session()
    s1.started_at = time.time() - 100
    p1 = write_session_json(s1, tmp_path)
    time.sleep(0.01)
    s2 = _make_session()
    p2 = write_session_json(s2, tmp_path)

    resolved = resolve_session(tmp_path)
    # Filenames sort lexically; newer timestamp = newer name = first.
    assert resolved is not None
    assert resolved.stem == p2.stem


def test_resolve_explicit_id(tmp_path: Path) -> None:
    session = _make_session()
    path = write_session_json(session, tmp_path)
    resolved = resolve_session(tmp_path, path.stem)
    assert resolved == path


def test_resolve_by_prefix(tmp_path: Path) -> None:
    session = _make_session()
    path = write_session_json(session, tmp_path)
    # Use first 8 chars (date) as prefix.
    prefix = path.stem[:8]
    resolved = resolve_session(tmp_path, prefix)
    assert resolved == path


def test_resolve_no_match_returns_none(tmp_path: Path) -> None:
    """Empty dir and missing selector both → None."""
    assert resolve_session(tmp_path) is None
    session = _make_session()
    write_session_json(session, tmp_path)
    assert resolve_session(tmp_path, "nonexistent-99999999") is None
