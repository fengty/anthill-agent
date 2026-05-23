"""0.2.34 — Functional QA orchestration.

The /test command's two hardest pieces are parsing model output:
  1. Cases come back as JSON wrapped in markdown / preamble.
  2. Verdicts come back inline with the citizen's narrative.

Both parsers need to be defensive — models violate the strict
contract often enough that brittleness here would defeat the whole
flow.

Tests also cover requirement loading (file vs inline), the report
writer, and the prompt builders.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from anthill.core.qa import (
    CASE_EXECUTION_PROMPT,
    CASE_GENERATION_PROMPT,
    TestCase,
    TestResult,
    TestSession,
    build_execution_prompt,
    format_report,
    load_requirement,
    parse_cases_response,
    parse_verdict,
    reports_dir,
    write_report,
)


# --- parse_cases_response: handles model misbehavior ---------------


def test_parse_clean_json() -> None:
    """Happy path: pure JSON."""
    text = '''{
        "cases": [
            {"name": "login works",
             "prerequisites": "user account",
             "steps": ["go to /login", "enter creds"],
             "expected": "/dashboard appears",
             "verification": "url=/dashboard"}
        ]
    }'''
    cases = parse_cases_response(text)
    assert len(cases) == 1
    assert cases[0].name == "login works"
    assert cases[0].id == 1
    assert cases[0].steps == ["go to /login", "enter creds"]


def test_parse_strips_markdown_fence() -> None:
    """Models love to wrap JSON in ```json ... ```."""
    text = '''Here are your test cases:

```json
{
    "cases": [
        {"name": "case A", "steps": ["x"], "expected": "y"}
    ]
}
```
'''
    cases = parse_cases_response(text)
    assert len(cases) == 1
    assert cases[0].name == "case A"


def test_parse_strips_preamble_and_trailing_text() -> None:
    """Some models pad with explanation before/after the JSON."""
    text = (
        "Sure, here you go:\n\n"
        '{"cases": [{"name": "A", "steps": [], "expected": "ok"}]}'
        "\n\nLet me know if you need anything else."
    )
    cases = parse_cases_response(text)
    assert len(cases) == 1


def test_parse_unparseable_returns_empty() -> None:
    """Garbage in → empty list out, not a crash."""
    assert parse_cases_response("") == []
    assert parse_cases_response("hi there") == []
    assert parse_cases_response("{not valid json") == []


def test_parse_handles_nested_objects_in_steps() -> None:
    """The brace-counting parser handles nested {} inside the cases array."""
    text = '''{
        "cases": [
            {"name": "complex", "steps": [], "expected": "ok",
             "verification": "JSON match: {\\"status\\": 200}"}
        ]
    }'''
    cases = parse_cases_response(text)
    assert len(cases) == 1


def test_parse_skips_cases_without_name() -> None:
    """Defensive: a malformed entry without a name is dropped, not crash."""
    text = '{"cases": [{"name": "ok"}, {"steps": ["x"]}, {"name": "also ok"}]}'
    cases = parse_cases_response(text)
    assert [c.name for c in cases] == ["ok", "also ok"]


# --- parse_verdict ----------------------------------------------------


def test_verdict_pass() -> None:
    text = "I ran the test.\nstdout: OK\nVERDICT: PASS"
    status, reason = parse_verdict(text)
    assert status == "passed"


def test_verdict_fail_with_reason() -> None:
    text = "checked the dashboard.\nVERDICT: FAIL element not found"
    status, reason = parse_verdict(text)
    assert status == "failed"
    assert "element not found" in reason


def test_verdict_case_insensitive() -> None:
    """Models may emit verdict in mixed case."""
    text = "doing stuff.\nverdict: pass"
    status, _ = parse_verdict(text)
    assert status == "passed"


def test_verdict_missing_is_errored() -> None:
    """No VERDICT line → errored (citizen didn't follow protocol)."""
    text = "I did some stuff but forgot the verdict."
    status, reason = parse_verdict(text)
    assert status == "errored"


def test_verdict_last_match_wins() -> None:
    """A model that wrote 'VERDICT: PASS' mid-monologue then 'VERDICT:
    FAIL' at the end should be FAIL."""
    text = (
        "Plan: I expect VERDICT: PASS if everything works.\n"
        "Actually ran it.\n"
        "VERDICT: FAIL because timeout"
    )
    status, _ = parse_verdict(text)
    assert status == "failed"


# --- requirement loading ---------------------------------------------


def test_load_inline_requirement() -> None:
    text, label = load_requirement("test the login flow")
    assert text == "test the login flow"
    assert "inline" in label.lower()


def test_load_from_file(tmp_path: Path) -> None:
    p = tmp_path / "prd.md"
    p.write_text("# Login\nUser logs in with email/password.")
    text, label = load_requirement(f"@{p}")
    assert "Login" in text
    assert str(p) in label


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    text, label = load_requirement(f"@{tmp_path}/does-not-exist.md")
    assert text == ""
    assert "failed" in label


def test_load_empty_source() -> None:
    text, label = load_requirement("")
    assert text == ""


# --- prompt builders ------------------------------------------------


def test_execution_prompt_includes_case_details() -> None:
    c = TestCase(
        id=3,
        name="login fails on bad password",
        prerequisites="account exists",
        steps=["go to /login", "type bad password", "submit"],
        expected="error visible",
        verification="text=Invalid credentials",
    )
    prompt = build_execution_prompt(c)
    # Each field must be reachable in the prompt the citizen sees.
    assert "#3" in prompt
    assert "login fails on bad password" in prompt
    assert "account exists" in prompt
    assert "type bad password" in prompt
    assert "error visible" in prompt
    assert "Invalid credentials" in prompt
    # Verdict instruction is there.
    assert "VERDICT:" in prompt


def test_generation_prompt_includes_requirement() -> None:
    """The CASE_GENERATION_PROMPT has a placeholder; verify it can
    be substituted with a real requirement."""
    rendered = CASE_GENERATION_PROMPT.replace(
        "{requirement}", "users must log in"
    )
    assert "users must log in" in rendered
    assert "JSON" in rendered  # output format spec


# --- report writer --------------------------------------------------


def test_report_renders_pass_fail_mix(tmp_path: Path) -> None:
    case1 = TestCase(id=1, name="login works", expected="dashboard")
    case2 = TestCase(id=2, name="bad password fails", expected="error")
    session = TestSession(
        requirement="users must log in correctly",
        cases=[case1, case2],
        nation_name="t",
    )
    session.results = [
        TestResult(
            case=case1, status="passed",
            narrative="ran login, got dashboard. VERDICT: PASS",
            duration_seconds=2.5, actions_taken=4,
        ),
        TestResult(
            case=case2, status="failed",
            narrative="error didn't show. VERDICT: FAIL element missing",
            duration_seconds=1.8, actions_taken=3,
            error="element missing",
        ),
    ]
    session.ended_at = session.started_at + 5

    text = format_report(session)

    # Both icons present.
    assert "✅" in text and "❌" in text
    # Both case names present.
    assert "login works" in text
    assert "bad password fails" in text
    # Failure reason surfaces.
    assert "element missing" in text
    # Pass rate.
    assert "1/2" in text


def test_write_report_creates_file(tmp_path: Path) -> None:
    case = TestCase(id=1, name="一个测试")
    session = TestSession(
        requirement="x", cases=[case], nation_name="t",
        results=[TestResult(case=case, status="passed", duration_seconds=0.1)],
        ended_at=time.time(),
    )
    path = write_report(session, tmp_path)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "一个测试" in content
    # Filename has a timestamp.
    assert path.name.endswith(".md")
    # Lives under <home>/test_reports.
    assert path.parent == reports_dir(tmp_path)


def test_report_handles_empty_results(tmp_path: Path) -> None:
    """No cases run → still produces a valid (if sparse) report."""
    session = TestSession(requirement="x", cases=[], nation_name="t")
    text = format_report(session)
    assert "# Test Report" in text
    assert "0/0" in text  # pass rate
