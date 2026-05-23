"""0.2.38 — JUnit XML output for CI integration.

`anthill test ... --junit-xml=results.xml` writes a JUnit-format
XML file that CI tools (GitHub Actions, GitLab CI, Jenkins,
CircleCI) parse natively. The format is decades-old and stable;
our generator just needs to produce well-formed XML with the
right elements + attributes.

Tests verify the XML output against the standard schema:
  - <testsuite> wrapper with name/tests/failures/errors/skipped/time
  - <testcase> per result with classname/name/time
  - <failure> for failed cases (with message + body)
  - <error> for errored cases
  - <skipped/> bare element
  - XML special chars escaped (the model produces wild output)
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from anthill.core.qa import (
    TestCase,
    TestResult,
    TestSession,
    format_junit_xml,
    write_junit_xml,
)


def _result(name: str, status: str, *, error: str = None) -> TestResult:
    return TestResult(
        case=TestCase(id=1, name=name, expected="x"),
        status=status,
        narrative=f"{name} narrative",
        duration_seconds=0.5,
        error=error,
    )


def _session(results: list[TestResult]) -> TestSession:
    return TestSession(
        requirement="r",
        cases=[r.case for r in results],
        results=results,
        nation_name="t",
        ended_at=time.time(),
    )


# --- structure -------------------------------------------------------


def test_junit_xml_parses_as_valid_xml() -> None:
    """The output is well-formed XML."""
    s = _session([_result("login works", "passed")])
    xml_str = format_junit_xml(s)
    # If this parses without ParseError, we're good.
    root = ET.fromstring(xml_str)
    assert root.tag == "testsuite"


def test_junit_xml_summary_attributes() -> None:
    """testsuite root has tests/failures/errors/skipped attributes."""
    results = [
        _result("a", "passed"),
        _result("b", "failed", error="no element"),
        _result("c", "errored", error="provider timeout"),
        _result("d", "skipped"),
    ]
    xml_str = format_junit_xml(_session(results))
    root = ET.fromstring(xml_str)
    assert root.attrib["tests"] == "4"
    assert root.attrib["failures"] == "1"
    assert root.attrib["errors"] == "1"
    assert root.attrib["skipped"] == "1"


def test_junit_xml_one_testcase_per_result() -> None:
    """N results → N <testcase> children."""
    results = [_result(f"case-{i}", "passed") for i in range(5)]
    xml_str = format_junit_xml(_session(results))
    root = ET.fromstring(xml_str)
    cases = root.findall("testcase")
    assert len(cases) == 5


def test_junit_passed_case_has_no_failure_child() -> None:
    """Passed = bare <testcase/> — no nested failure/error/skipped."""
    s = _session([_result("good", "passed")])
    xml_str = format_junit_xml(s)
    root = ET.fromstring(xml_str)
    tc = root.find("testcase")
    assert tc.find("failure") is None
    assert tc.find("error") is None
    assert tc.find("skipped") is None


def test_junit_failed_case_has_failure_child() -> None:
    s = _session([_result("bad", "failed", error="assertion failed")])
    xml_str = format_junit_xml(s)
    root = ET.fromstring(xml_str)
    tc = root.find("testcase")
    failure = tc.find("failure")
    assert failure is not None
    assert "assertion failed" in failure.attrib["message"]


def test_junit_errored_case_has_error_child() -> None:
    s = _session([_result("oops", "errored", error="provider died")])
    xml_str = format_junit_xml(s)
    root = ET.fromstring(xml_str)
    tc = root.find("testcase")
    err = tc.find("error")
    assert err is not None
    assert "provider died" in err.attrib["message"]


# --- escaping --------------------------------------------------------


def test_junit_escapes_xml_special_chars_in_name() -> None:
    """Test names can contain <, >, & — model output is unconstrained."""
    s = _session([_result("test <login> & validate", "passed")])
    xml_str = format_junit_xml(s)
    # ParseError if we didn't escape.
    root = ET.fromstring(xml_str)
    tc = root.find("testcase")
    # The attribute value, after XML decoding, equals the original.
    assert tc.attrib["name"] == "test <login> & validate"


def test_junit_escapes_xml_special_chars_in_failure_body() -> None:
    """Narrative often has shell snippets with <, >, &; must escape."""
    result = _result("bad", "failed", error="reason")
    result.narrative = "command: cat /etc/hosts | grep <ip> && echo done"
    s = _session([result])
    xml_str = format_junit_xml(s)
    root = ET.fromstring(xml_str)  # must not raise
    failure = root.find("testcase/failure")
    assert failure is not None
    # The text inside <failure> is the original after XML decoding.
    assert "<ip>" in failure.text


# --- file write ------------------------------------------------------


def test_write_junit_xml_creates_file(tmp_path: Path) -> None:
    """write_junit_xml writes a parseable file at the given path."""
    s = _session([_result("a", "passed"), _result("b", "failed", error="x")])
    out = tmp_path / "results.xml"
    written = write_junit_xml(s, out)
    assert written == out
    assert out.exists()
    root = ET.parse(out).getroot()
    assert root.attrib["tests"] == "2"
    assert root.attrib["failures"] == "1"


def test_write_junit_xml_creates_parent_dirs(tmp_path: Path) -> None:
    """Path like ci/out/junit.xml under a fresh tmp_path: parents
    get mkdir'd automatically."""
    s = _session([_result("a", "passed")])
    nested = tmp_path / "ci" / "out" / "junit.xml"
    write_junit_xml(s, nested)
    assert nested.exists()


# --- duration math --------------------------------------------------


def test_junit_total_time_is_session_duration() -> None:
    """testsuite/@time = ended_at - started_at."""
    s = _session([_result("a", "passed")])
    s.started_at = 1000.0
    s.ended_at = 1005.5
    xml_str = format_junit_xml(s)
    root = ET.fromstring(xml_str)
    assert float(root.attrib["time"]) == 5.5


def test_junit_per_case_time_is_case_duration() -> None:
    r = _result("a", "passed")
    r.duration_seconds = 3.14
    xml_str = format_junit_xml(_session([r]))
    root = ET.fromstring(xml_str)
    tc = root.find("testcase")
    assert float(tc.attrib["time"]) == 3.14
