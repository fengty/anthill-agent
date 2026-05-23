"""0.2.35 — fix-test-rerun loop: failed tests get auto-repair attempts.

After /test produces failures, --fix N invokes a citizen agent to
diagnose + apply a fix, then re-runs the case. Up to N attempts.
Tests cover the prompt builders, verdict parsing, and result
recording semantics. Real end-to-end via REPL is exercised by
hand; here we test the pure logic.
"""

from __future__ import annotations

from anthill.core.qa import (
    CASE_FIX_PROMPT,
    FixAttempt,
    TestCase,
    TestResult,
    TestSession,
    build_fix_prompt,
    format_report,
    parse_fix_verdict,
)


def _failed_result() -> TestResult:
    return TestResult(
        case=TestCase(
            id=1,
            name="login fails on bad password",
            expected="error visible",
        ),
        status="failed",
        narrative="tried bad password, no error shown. VERDICT: FAIL no error element",
        error="no error element",
    )


# --- prompt building ----------------------------------------------------


def test_fix_prompt_includes_failure_signal() -> None:
    """The citizen running the fix must see WHY the test failed."""
    prompt = build_fix_prompt(_failed_result())
    assert "login fails on bad password" in prompt
    assert "no error element" in prompt
    assert "VERDICT: FAIL" in prompt
    # And the FIXED/UNFIXABLE contract is in there.
    assert "FIXED:" in prompt
    assert "UNFIXABLE:" in prompt


def test_fix_prompt_warns_against_self_rerunning() -> None:
    """The orchestrator re-runs after fix; the citizen shouldn't
    duplicate that effort (wastes tokens, may corrupt state)."""
    prompt = build_fix_prompt(_failed_result())
    # Some signal that the citizen shouldn't re-run.
    p = prompt.lower()
    assert "do not re-run" in p or "the orchestrator" in p


# --- verdict parsing ----------------------------------------------------


def test_fix_verdict_fixed() -> None:
    text = "I edited login.py to add the error element.\nFIXED: added .error-msg to template"
    status, summary = parse_fix_verdict(text)
    assert status == "fixed"
    assert "error-msg" in summary


def test_fix_verdict_unfixable() -> None:
    text = "couldn't find any code. UNFIXABLE: source not on disk"
    status, summary = parse_fix_verdict(text)
    assert status == "unfixable"
    assert "source not on disk" in summary


def test_fix_verdict_missing_is_unknown() -> None:
    """Citizen forgot the verdict line → unknown (retry-eligible)."""
    text = "I poked around but didn't conclude anything."
    status, _ = parse_fix_verdict(text)
    assert status == "unknown"


def test_fix_verdict_last_match_wins() -> None:
    """If model planned 'I might say FIXED:' mid-monologue then concluded
    differently at the end, the LAST verdict line is canonical."""
    text = (
        "I plan to say FIXED: changes applied.\n"
        "But on closer inspection no fix possible.\n"
        "UNFIXABLE: missing access"
    )
    status, _ = parse_fix_verdict(text)
    assert status == "unfixable"


def test_fix_verdict_case_insensitive() -> None:
    text = "did it.\nfixed: small change"
    status, _ = parse_fix_verdict(text)
    assert status == "fixed"


# --- TestResult / FixAttempt data model --------------------------------


def test_fix_attempts_default_empty() -> None:
    """Old TestResults without fix loop still serialize/render fine."""
    r = TestResult(case=TestCase(id=1, name="x"), status="passed")
    assert r.fix_attempts == []


def test_fix_attempts_recorded_on_result() -> None:
    """Adding FixAttempts to a TestResult preserves them."""
    r = TestResult(case=TestCase(id=1, name="x"), status="failed")
    r.fix_attempts.append(FixAttempt(
        attempt=1, fix_status="fixed", fix_summary="patched login.py",
        rerun_status="passed", duration_seconds=4.2,
    ))
    assert len(r.fix_attempts) == 1
    assert r.fix_attempts[0].rerun_status == "passed"


# --- report renders fix loop ------------------------------------------


def test_report_shows_fix_attempts_trace() -> None:
    """When fix attempts were made, the report includes a clear
    trace: each attempt's fix status + rerun result."""
    case = TestCase(id=1, name="login fails on bad password")
    result = TestResult(
        case=case,
        status="passed",  # eventually fixed
        narrative="...",
        duration_seconds=3.5,
        actions_taken=5,
        fix_attempts=[
            FixAttempt(
                attempt=1, fix_status="unknown",
                fix_summary="no FIXED line",
                rerun_status="skipped", duration_seconds=2.0,
            ),
            FixAttempt(
                attempt=2, fix_status="fixed",
                fix_summary="added .error-msg element",
                rerun_status="passed", duration_seconds=4.1,
            ),
        ],
    )
    session = TestSession(
        requirement="login should show errors",
        cases=[case], results=[result], nation_name="t",
    )
    text = format_report(session)
    assert "Fix attempts:" in text
    assert "attempt 1" in text
    assert "attempt 2" in text
    assert "added .error-msg element" in text


def test_report_works_without_fix_attempts() -> None:
    """Existing reports (no --fix) still render cleanly."""
    case = TestCase(id=1, name="x")
    result = TestResult(case=case, status="passed", duration_seconds=1.0)
    session = TestSession(
        requirement="r", cases=[case], results=[result], nation_name="t",
    )
    text = format_report(session)
    assert "Fix attempts:" not in text  # section absent
    assert "✅" in text  # but pass icon still there
