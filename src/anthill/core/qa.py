"""0.2.34 — Functional QA: 需求 → 测试用例 → 跑 → 报告.

User's pain point was: "我需要的是功能测试，像是测试人员做的
一样，分析需求，测试用例，准备数据，点击界面，查询数据库，
写测试报告这一系列的操作."

By 0.2.32 anthill had all the primitives:
  - native tool_use API → models call tools reliably
  - [[bash:]] / browser_action → operate machine & UI
  - kanban → track work across sessions
  - delegate_task → fan out to specialists

This module is the FUNCTIONAL TEST orchestrator that wires those
together as a coherent flow:

  1. Parse requirement source (inline text / @file / URL)
  2. Generate test cases via QA-prompted LLM call
  3. Show cases to user, ask which to run
  4. Execute each case via an agentic citizen run
  5. Each citizen drives bash + browser tools to actually test
  6. Write a markdown report with PASS/FAIL + evidence

The QA prompt is the most important piece. Models default to
"here's a test PLAN" tutorials when asked about testing; we need
"do the test NOW, return only structured JSON" output.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


# --- data shapes ------------------------------------------------------


@dataclass
class TestCase:
    """One executable test scenario."""

    # Tell pytest this isn't a test class (Test* triggers collection).
    __test__ = False

    id: int
    name: str
    prerequisites: str = ""
    steps: list[str] = field(default_factory=list)
    expected: str = ""
    verification: str = ""

    @classmethod
    def from_dict(cls, idx: int, d: dict) -> "TestCase":
        return cls(
            id=idx,
            name=str(d.get("name", f"case-{idx}")).strip(),
            prerequisites=str(d.get("prerequisites", "")).strip(),
            steps=[str(s).strip() for s in (d.get("steps", []) or []) if s],
            expected=str(d.get("expected", "")).strip(),
            verification=str(d.get("verification", "")).strip(),
        )


@dataclass
class FixAttempt:
    """One fix-then-rerun cycle for a failed test (0.2.35)."""

    attempt: int               # 1-based attempt counter
    fix_status: str            # fixed / unfixable / unknown
    fix_summary: str           # what the citizen claims they did
    rerun_status: str          # passed / failed / errored (after fix)
    rerun_narrative: str = ""
    duration_seconds: float = 0.0


@dataclass
class TestResult:
    """Outcome of running one TestCase."""

    __test__ = False  # not a pytest test class

    case: TestCase
    status: str  # passed / failed / skipped / errored
    narrative: str = ""        # citizen's final text response
    duration_seconds: float = 0.0
    actions_taken: int = 0     # bash + browser calls count
    evidence: list[str] = field(default_factory=list)  # screenshot paths, etc
    error: Optional[str] = None
    fix_attempts: list[FixAttempt] = field(default_factory=list)  # 0.2.35


@dataclass
class TestSession:
    """One /test invocation as a whole."""

    __test__ = False  # not a pytest test class

    requirement: str
    cases: list[TestCase]
    results: list[TestResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    nation_name: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def total(self) -> int:
        return len(self.results)


# --- QA prompts ------------------------------------------------------


# The case-generation prompt. STRICT instructions to output JSON — no
# preamble, no markdown wrapper. Models love to violate this; we
# parse defensively (see parse_cases_response).
# 0.2.45 — exploration prompt for sparse-requirement-with-URL cases.
# Real bug: user typed `/test 我需要进行测试 http://localhost:3000/`,
# model couldn't see localhost (it's on the LLM provider, not the
# user's machine), returned `[]`. Now anthill first asks a citizen
# to USE the browser tool to inspect the URL, captures the report,
# and prepends it to the requirement so case-gen has context.
EXPLORE_FOR_QA_PROMPT = """\
You are a QA scout. The king wants to test {url} but hasn't
described what's on it. Your job: USE the browser tool to LOOK at
the page, then report what you see in compact form so a downstream
QA planner can write meaningful test cases.

Do this:
  1. [[browser:goto {url}]]
  2. [[browser:text body]]                   # or specific selectors
  3. (optional) [[browser:screenshot home]]  # visual reference

Then write a structured report covering:
  - PAGE TITLE: ...
  - VISIBLE NAV / MENU: ... (top nav items, sidebar, etc.)
  - PRIMARY FORMS / INPUTS: ... (login form? search? CRUD?)
  - PRIMARY BUTTONS / CTAs: ...
  - APPARENT USER FLOWS: 1-3 likely user journeys
  - AUTH NEEDED: yes/no, what credentials field names if visible
  - LIKELY TEST SCENARIOS: 3-5 candidates (don't write the cases —
    just list the headlines)

Keep the report under 500 words. Be CONCRETE, name actual UI
labels you saw, not generic guesses.
"""


def is_sparse_requirement_with_url(text: str) -> Optional[str]:
    """0.2.45 — detect "vague intent + URL" shape that needs exploration.

    Returns the URL if the requirement is sparse (≤ 30 substantive
    chars beyond the URL) and contains an http(s) link. Returns None
    when the user provided a rich requirement OR no URL.

    Examples that trigger:
      - "test http://localhost:3000/"
      - "http://x.com 帮我测试一下"
      - "我需要进行测试 http://localhost:3000/"

    Examples that don't:
      - "test the login flow on http://x.com: try wrong password,
         expect 'invalid credentials' visible"  (rich, no exploration needed)
      - "test the search feature"  (no URL, can't auto-explore)
    """
    if not text:
        return None
    # Extract URL.
    url_match = re.search(r"https?://[^\s,，\"<>]+", text)
    if not url_match:
        return None
    url = url_match.group(0).rstrip(".,;:!?）)")
    # Sparseness: strip URL + common verbs, see how much actual
    # specification is left.
    rest = text.replace(url_match.group(0), "")
    for verb in (
        "test", "测试", "检查", "看看", "看下", "进行", "我需要",
        "please", "帮我", "请",
    ):
        rest = rest.replace(verb, "")
    rest = "".join(rest.split())  # collapse whitespace
    # CJK chars count double (same as 0.2.41 info-density).
    info_len = sum(2 if 0x4E00 <= ord(c) <= 0x9FFF else 1 for c in rest)
    return url if info_len < 30 else None


CASE_GENERATION_PROMPT = """\
You are a senior functional QA engineer. Given the requirement
below, design 3-7 concrete, executable test cases.

For each test case provide:
  - "name": short imperative title (e.g. "登录成功后跳到首页")
  - "prerequisites": what must be true before running (env, data, login)
  - "steps": numbered concrete actions (UI clicks, API calls, db queries)
  - "expected": what success looks like (observable, specific)
  - "verification": how to programmatically check (selector text,
    DB row, HTTP status, file content — concrete, not "确认正确")

OUTPUT FORMAT — strict JSON, no markdown, no preamble:
{
  "cases": [
    {"name": "...", "prerequisites": "...", "steps": ["step 1", "step 2"],
     "expected": "...", "verification": "..."},
    ...
  ]
}

Focus on cases that exercise the REQUIREMENT, not generic smoke
tests. Each case should fail in a unique way if the system is broken.

REQUIREMENT:
=========================
{requirement}
=========================
"""


# Fix-loop prompt. After a test FAILED, this is what we send a
# citizen to try and fix the underlying issue. The citizen must
# diagnose, edit code, AND signal completion. We don't re-run the
# test in this prompt — the orchestrator does that separately.
CASE_FIX_PROMPT = """\
A functional test just FAILED on the king's system. Your job is to
diagnose the ROOT CAUSE and fix it.

Failed test case #{case_id}: {name}
Expected: {expected}
Failure reason: {failure_reason}

Failing test's narrative (what the test tried):
=========================
{narrative}
=========================

Your task:
  1. Read the narrative to understand what the test tried
  2. Use bash_run to inspect the relevant code / config / data
  3. Apply a concrete fix (edit file via bash, restart service,
     migrate data, whatever)
  4. End your response with exactly one of:
        FIXED: <one-line summary of what you changed>
        UNFIXABLE: <one-line reason>

DO NOT re-run the test yourself — the orchestrator will do that
after you finish. Focus on diagnosis and fix. Use [[bash:]] tools
to make CONCRETE changes; don't just describe what you would do.
"""


FIX_VERDICT_RE = re.compile(
    # Allow the verdict to appear anywhere — start of line OR mid-
    # sentence — since real model output sometimes drops it in flow
    # ("...investigation done. FIXED: small patch."). Word-boundary
    # at the start prevents matching "PREFIXED" etc.
    r"\b(?P<verdict>FIXED|UNFIXABLE)\s*:\s*(?P<reason>[^\n]+)",
    re.IGNORECASE,
)


def build_fix_prompt(result: "TestResult") -> str:
    """Render the fix-loop prompt for a failed TestResult."""
    return CASE_FIX_PROMPT.format(
        case_id=result.case.id,
        name=result.case.name,
        expected=result.case.expected or "(unspecified)",
        failure_reason=result.error or "unknown",
        narrative=(result.narrative or "(no narrative)").strip(),
    )


def parse_fix_verdict(text: str) -> tuple[str, str]:
    """Extract FIXED / UNFIXABLE + summary from citizen output.

    Returns (status, summary). status ∈ {"fixed", "unfixable", "unknown"}.
    Last match wins (same reason as parse_verdict).
    """
    if not text:
        return ("unknown", "no output")
    matches = list(FIX_VERDICT_RE.finditer(text))
    if not matches:
        return ("unknown", "no FIXED/UNFIXABLE line found")
    m = matches[-1]
    v = m.group("verdict").upper()
    reason = (m.group("reason") or "").strip()
    if v == "FIXED":
        return ("fixed", reason)
    return ("unfixable", reason)


# Per-case execution prompt. Citizens see this when running a single
# case in agentic mode. They use bash_run / browser_action to actually
# drive the test.
CASE_EXECUTION_PROMPT = """\
You are EXECUTING this functional test case on the king's actual
system. Use bash_run for shell / API / DB calls, browser_action for
UI. DO NOT describe — ACT. End with a final line containing exactly
"VERDICT: PASS" or "VERDICT: FAIL <one-line reason>".

Test case #{case_id}: {name}

Prerequisites:
{prerequisites}

Steps to execute:
{steps_block}

Expected outcome:
{expected}

How to verify:
{verification}

Drive the test now. Cite concrete evidence (command output, page
text, DB rows) in your narrative. End with the VERDICT line.
"""


def build_execution_prompt(case: TestCase) -> str:
    """Render a per-case prompt the citizen receives."""
    steps_block = "\n".join(
        f"  {i+1}. {s}" for i, s in enumerate(case.steps)
    ) or "  (no steps specified)"
    return CASE_EXECUTION_PROMPT.format(
        case_id=case.id,
        name=case.name,
        prerequisites=case.prerequisites or "(none)",
        steps_block=steps_block,
        expected=case.expected or "(unspecified)",
        verification=case.verification or "(unspecified)",
    )


# --- response parsing ------------------------------------------------


def parse_cases_response(text: str) -> list[TestCase]:
    """Pull `cases` out of the model's response.

    Defensive parsing: models often wrap JSON in markdown fences, add
    a preamble ("Here are the test cases:"), or trail an explanation.
    We strip fences, find the first `{...}` block, and try json.loads.
    Returns [] if nothing parseable.
    """
    if not text:
        return []
    # Strip ```json ... ``` fences.
    cleaned = re.sub(
        r"```(?:json)?\s*", "", text, flags=re.IGNORECASE
    ).replace("```", "")
    # Find the outermost JSON object using brace-counting.
    start = cleaned.find("{")
    if start < 0:
        return []
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return []
    raw = cleaned[start:end]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    cases_data = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases_data, list):
        return []
    return [
        TestCase.from_dict(idx + 1, d)
        for idx, d in enumerate(cases_data)
        if isinstance(d, dict) and d.get("name")
    ]


VERDICT_RE = re.compile(
    r"^\s*VERDICT\s*:\s*(?P<v>PASS|FAIL)\b(?P<reason>.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_verdict(text: str) -> tuple[str, str]:
    """Extract PASS/FAIL + reason from a citizen's case-execution output.

    Returns (status, reason). status ∈ {"passed", "failed", "errored"}.
    "errored" means no verdict line found at all — the citizen didn't
    follow protocol.
    """
    if not text:
        return ("errored", "no output")
    # Last match wins — sometimes models write VERDICT inside their
    # planning monologue then finalize at the end.
    matches = list(VERDICT_RE.finditer(text))
    if not matches:
        return ("errored", "no VERDICT line found")
    m = matches[-1]
    verdict = m.group("v").upper()
    reason = (m.group("reason") or "").strip()
    if verdict == "PASS":
        return ("passed", reason or "ok")
    return ("failed", reason or "failure")


# --- requirement loading ---------------------------------------------


def load_requirement(source: str, cwd: Path | None = None) -> tuple[str, str]:
    """Resolve a requirement source to its text.

    Returns (text, source_label).

    Recognized:
      - inline text (no @ or http prefix)
      - @<path> → read from file
      - http(s)://... → handled by caller (delegates to url_attachments)
    """
    source = source.strip()
    if not source:
        return ("", "")
    if source.startswith("@"):
        path = Path(source[1:]).expanduser()
        if cwd is not None and not path.is_absolute():
            path = cwd / path
        try:
            return (path.read_text(encoding="utf-8"), str(path))
        except OSError as e:
            return ("", f"(failed to read {path}: {e})")
    # http(s) is left to the caller — they have the url_attachments
    # machinery already wired up.
    return (source, "(inline)")


# --- report writer ---------------------------------------------------


def reports_dir(nation_dir: Path) -> Path:
    """The per-nation directory where test reports land."""
    return Path(nation_dir) / "test_reports"


def format_report(session: TestSession) -> str:
    """Render a TestSession as markdown."""
    when = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(session.started_at)
    )
    pass_rate = (
        f"{session.passed}/{session.total}" if session.total else "0/0"
    )
    duration = (session.ended_at or time.time()) - session.started_at

    lines = [
        f"# Test Report — {when}",
        "",
        f"**Nation:** {session.nation_name or '(unknown)'}",
        f"**Pass rate:** {pass_rate} (failed: {session.failed})",
        f"**Total duration:** {duration:.1f}s",
        "",
        "## Requirement",
        "",
        session.requirement.strip() or "(no requirement text)",
        "",
        "## Cases",
        "",
    ]

    for r in session.results:
        c = r.case
        icon = {"passed": "✅", "failed": "❌", "skipped": "⏭", "errored": "⚠️"}.get(
            r.status, "?"
        )
        lines.append(f"### {icon} #{c.id} — {c.name}")
        lines.append("")
        lines.append(f"- **Status:** {r.status}")
        lines.append(f"- **Duration:** {r.duration_seconds:.1f}s")
        lines.append(f"- **Tool calls:** {r.actions_taken}")
        if c.prerequisites:
            lines.append(f"- **Prerequisites:** {c.prerequisites}")
        if c.expected:
            lines.append(f"- **Expected:** {c.expected}")
        if r.error:
            lines.append(f"- **Error:** {r.error}")
        if r.evidence:
            lines.append("- **Evidence:**")
            for e in r.evidence:
                lines.append(f"  - {e}")
        if r.narrative:
            lines.append("")
            lines.append("**Narrative:**")
            lines.append("")
            lines.append(r.narrative.strip())
        # 0.2.35 — fix-loop trace.
        if r.fix_attempts:
            lines.append("")
            lines.append("**Fix attempts:**")
            lines.append("")
            for fa in r.fix_attempts:
                fix_icon = {"fixed": "🔧", "unfixable": "🚫", "unknown": "❓"}.get(
                    fa.fix_status, "?"
                )
                rerun_icon = {"passed": "✅", "failed": "❌", "errored": "⚠️"}.get(
                    fa.rerun_status, "?"
                )
                lines.append(
                    f"- attempt {fa.attempt}: {fix_icon} {fa.fix_status} "
                    f"({fa.fix_summary[:80]}) → rerun {rerun_icon} "
                    f"{fa.rerun_status}"
                )
        lines.append("")

    return "\n".join(lines)


def _session_slug(session: "TestSession") -> str:
    """Filesystem-safe slug derived from the first case name or
    requirement. Used as suffix on report + json filenames."""
    if session.cases:
        slug_src = session.cases[0].name
    else:
        slug_src = session.requirement[:40] or "test-session"
    slug = re.sub(r"[^\w一-鿿]+", "-", slug_src).strip("-").lower()[:30]
    return slug or "session"


def _session_id(session: "TestSession") -> str:
    """Stable identifier: '<YYYYMMDD-HHMMSS>-<slug>'."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(session.started_at))
    return f"{stamp}-{_session_slug(session)}"


# --- 0.2.38 — JUnit XML for CI integration ---------------------------


def format_junit_xml(session: "TestSession") -> str:
    """Render a TestSession as JUnit XML so CI tools (GitHub Actions,
    GitLab CI, Jenkins, etc.) can ingest the results natively.

    Schema: testsuite with N testcase children.
      - passed case → bare <testcase/>
      - failed case → <testcase><failure message="..."/></testcase>
      - errored case → <testcase><error message="..."/></testcase>
      - skipped case → <testcase><skipped/></testcase>
    """
    from xml.sax.saxutils import escape, quoteattr

    failures = sum(1 for r in session.results if r.status == "failed")
    errors = sum(1 for r in session.results if r.status == "errored")
    skipped = sum(1 for r in session.results if r.status == "skipped")
    total = len(session.results)
    duration = (session.ended_at or time.time()) - session.started_at

    when = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.localtime(session.started_at)
    )
    suite_name = "anthill.qa"
    if session.nation_name:
        suite_name = f"anthill.{session.nation_name}"

    out: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    out.append(
        f'<testsuite name={quoteattr(suite_name)} '
        f'tests="{total}" failures="{failures}" errors="{errors}" '
        f'skipped="{skipped}" time="{duration:.3f}" timestamp="{when}">'
    )
    for r in session.results:
        c = r.case
        case_name = escape(c.name)
        classname = escape(suite_name)
        time_s = r.duration_seconds
        out.append(
            f'  <testcase classname={quoteattr(classname)} '
            f'name={quoteattr(c.name)} time="{time_s:.3f}">'
        )
        if r.status == "failed":
            msg = (r.error or "test failed").replace("\n", " ")[:300]
            body = (r.narrative or "")[:5000]
            out.append(
                f'    <failure message={quoteattr(msg)} type="AssertionError">'
                f'{escape(body)}</failure>'
            )
        elif r.status == "errored":
            msg = (r.error or "executor errored").replace("\n", " ")[:300]
            body = (r.narrative or "")[:5000]
            out.append(
                f'    <error message={quoteattr(msg)} type="ExecutionError">'
                f'{escape(body)}</error>'
            )
        elif r.status == "skipped":
            out.append('    <skipped/>')
        out.append('  </testcase>')
    out.append('</testsuite>')
    return "\n".join(out)


def write_junit_xml(session: "TestSession", path: Path) -> Path:
    """Write the JUnit XML to `path`. Caller picks the path (CI tools
    expect specific filenames like `junit.xml` or `test-results.xml`)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(format_junit_xml(session), encoding="utf-8")
    return Path(path)


def write_report(session: "TestSession", nation_dir: Path) -> Path:
    """Write the markdown report and return the path."""
    d = reports_dir(nation_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_session_id(session)}.md"
    path.write_text(format_report(session), encoding="utf-8")
    return path


# --- 0.2.36 — JSON persistence + rehydration ------------------------


def write_session_json(session: "TestSession", nation_dir: Path) -> Path:
    """Persist the structured session next to the markdown report.

    Markdown is for humans; JSON is so /retest can rehydrate the
    cases (with their full execution narrative + verdicts) into
    Python and re-run failures without re-running the whole flow."""
    d = reports_dir(nation_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_session_id(session)}.json"
    payload = {
        "id": _session_id(session),
        "requirement": session.requirement,
        "nation_name": session.nation_name,
        "started_at": session.started_at,
        "ended_at": session.ended_at,
        "cases": [
            {
                "id": c.id,
                "name": c.name,
                "prerequisites": c.prerequisites,
                "steps": c.steps,
                "expected": c.expected,
                "verification": c.verification,
            }
            for c in session.cases
        ],
        "results": [
            {
                "case_id": r.case.id,
                "status": r.status,
                "narrative": r.narrative,
                "duration_seconds": r.duration_seconds,
                "actions_taken": r.actions_taken,
                "evidence": list(r.evidence),
                "error": r.error,
                "fix_attempts": [
                    {
                        "attempt": fa.attempt,
                        "fix_status": fa.fix_status,
                        "fix_summary": fa.fix_summary,
                        "rerun_status": fa.rerun_status,
                        "rerun_narrative": fa.rerun_narrative,
                        "duration_seconds": fa.duration_seconds,
                    }
                    for fa in r.fix_attempts
                ],
            }
            for r in session.results
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_session_json(path: Path) -> "TestSession":
    """Rehydrate a TestSession from JSON. Raises FileNotFoundError /
    ValueError on bad input — caller handles."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [
        TestCase(
            id=c["id"],
            name=c["name"],
            prerequisites=c.get("prerequisites", ""),
            steps=list(c.get("steps", [])),
            expected=c.get("expected", ""),
            verification=c.get("verification", ""),
        )
        for c in data.get("cases", [])
    ]
    by_id = {c.id: c for c in cases}
    results = []
    for r in data.get("results", []):
        case = by_id.get(r["case_id"])
        if case is None:
            continue
        results.append(TestResult(
            case=case,
            status=r["status"],
            narrative=r.get("narrative", ""),
            duration_seconds=r.get("duration_seconds", 0.0),
            actions_taken=r.get("actions_taken", 0),
            evidence=list(r.get("evidence", [])),
            error=r.get("error"),
            fix_attempts=[
                FixAttempt(
                    attempt=fa["attempt"],
                    fix_status=fa["fix_status"],
                    fix_summary=fa.get("fix_summary", ""),
                    rerun_status=fa.get("rerun_status", "unknown"),
                    rerun_narrative=fa.get("rerun_narrative", ""),
                    duration_seconds=fa.get("duration_seconds", 0.0),
                )
                for fa in r.get("fix_attempts", [])
            ],
        ))
    return TestSession(
        requirement=data.get("requirement", ""),
        cases=cases,
        results=results,
        started_at=data.get("started_at", time.time()),
        ended_at=data.get("ended_at"),
        nation_name=data.get("nation_name", ""),
    )


@dataclass
class SessionMeta:
    """Compact descriptor for the /test history listing."""

    id: str           # YYYYMMDD-HHMMSS-slug (filename stem)
    path: Path        # the .json file
    started_at: float
    requirement_preview: str
    total: int
    passed: int
    failed: int


def list_sessions(nation_dir: Path, limit: int = 30) -> list[SessionMeta]:
    """List recent test sessions, newest first. Reads JSON files
    in reports_dir/ — robust to partial files (skips unparseable)."""
    d = reports_dir(nation_dir)
    if not d.exists():
        return []
    metas: list[SessionMeta] = []
    for path in sorted(d.glob("*.json"), reverse=True)[:limit]:
        try:
            sess = load_session_json(path)
        except Exception:  # noqa: BLE001 — skip corrupt files
            continue
        metas.append(SessionMeta(
            id=path.stem,
            path=path,
            started_at=sess.started_at,
            requirement_preview=sess.requirement.strip().splitlines()[0][:80]
                if sess.requirement.strip() else "(no requirement)",
            total=len(sess.results),
            passed=sum(1 for r in sess.results if r.status == "passed"),
            failed=sum(1 for r in sess.results if r.status == "failed"),
        ))
    return metas


# --- 0.2.39 — data-driven test cases (template × rows) ---------------


_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass
class CaseTemplate:
    """A test case with {placeholder} slots, expanded over N data rows."""

    __test__ = False  # not a pytest test class

    name: str          # e.g. "{scenario}: login with {email}"
    prerequisites: str = ""
    steps: list[str] = field(default_factory=list)
    expected: str = ""
    verification: str = ""

    def required_placeholders(self) -> set[str]:
        """All distinct {placeholder} names referenced by this template."""
        keys: set[str] = set()
        for txt in [self.name, self.prerequisites, self.expected,
                    self.verification, *self.steps]:
            keys.update(_PLACEHOLDER_RE.findall(txt or ""))
        return keys


@dataclass
class DataTable:
    """A template + a list of data rows to expand it across."""

    __test__ = False

    template: CaseTemplate
    rows: list[dict[str, str]]


def load_data_table(path: Path) -> DataTable:
    """Parse a YAML / JSON file into (template, rows).

    Schema (YAML or JSON):
      template:
        name:          "{scenario}: login with {email}"
        prerequisites: "user account exists"
        steps:
          - "open /login"
          - "type {email} into #email"
          - "type {password} into #password"
          - "click submit"
        expected:      "{expected_outcome}"
        verification:  "{verify_step}"
      rows:
        - scenario: "正确密码"
          email: "user@x.com"
          ...
        - ...

    Raises ValueError on schema problems with a specific message.
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as e:
            raise ValueError(
                f"YAML file ({p.name}) requires PyYAML. Install with "
                f"`pip install pyyaml`, or use JSON."
            ) from e
        data = yaml.safe_load(raw)
    elif suffix == ".json":
        data = json.loads(raw)
    else:
        raise ValueError(
            f"unknown data file format: {p.suffix!r}. Use .yaml/.yml/.json."
        )
    if not isinstance(data, dict):
        raise ValueError(
            f"{p.name}: top-level must be a mapping with `template:` and `rows:`"
        )
    tpl_data = data.get("template")
    rows_data = data.get("rows")
    if not isinstance(tpl_data, dict):
        raise ValueError(f"{p.name}: missing or invalid `template:` block")
    if not isinstance(rows_data, list) or not rows_data:
        raise ValueError(
            f"{p.name}: `rows:` must be a non-empty list"
        )
    template = CaseTemplate(
        name=str(tpl_data.get("name", "")).strip(),
        prerequisites=str(tpl_data.get("prerequisites", "")).strip(),
        steps=[str(s).strip() for s in (tpl_data.get("steps") or []) if s],
        expected=str(tpl_data.get("expected", "")).strip(),
        verification=str(tpl_data.get("verification", "")).strip(),
    )
    if not template.name:
        raise ValueError(f"{p.name}: template.name is required")

    rows: list[dict[str, str]] = []
    for i, r in enumerate(rows_data, start=1):
        if not isinstance(r, dict):
            raise ValueError(
                f"{p.name}: row {i} must be a mapping, got {type(r).__name__}"
            )
        rows.append({str(k): str(v) for k, v in r.items()})

    # Validate that every placeholder in template has a key in EVERY row.
    needed = template.required_placeholders()
    for i, row in enumerate(rows, start=1):
        missing = needed - set(row.keys())
        if missing:
            raise ValueError(
                f"{p.name}: row {i} missing keys: {sorted(missing)}"
            )

    return DataTable(template=template, rows=rows)


def expand_data_cases(table: DataTable) -> list[TestCase]:
    """Materialize template × rows into concrete TestCase instances.

    Substitution uses str.format(**row) so {placeholder} fills with
    row values. IDs are assigned 1-based, in row order. Missing
    placeholders should already have been caught by load_data_table;
    here we raise KeyError if they slip through (programmer error).
    """
    cases: list[TestCase] = []
    for i, row in enumerate(table.rows, start=1):
        try:
            c = TestCase(
                id=i,
                name=table.template.name.format(**row),
                prerequisites=table.template.prerequisites.format(**row),
                steps=[s.format(**row) for s in table.template.steps],
                expected=table.template.expected.format(**row),
                verification=table.template.verification.format(**row),
            )
        except KeyError as e:
            raise ValueError(
                f"row {i}: missing placeholder {e!s}. "
                f"row keys={sorted(row.keys())}, "
                f"template needs={sorted(table.template.required_placeholders())}"
            ) from e
        cases.append(c)
    return cases


# --- 0.2.37 — cross-session trend aggregation -------------------------


@dataclass
class CaseStats:
    """Stability stats for one case (matched by name) across sessions."""

    name: str
    runs: int                  # how many sessions ran this case
    passed: int                # how many of those passed
    last_status: str           # status of the most recent run
    last_seen: float           # most recent session timestamp
    last_error: Optional[str] = None  # error from the most recent failure
    first_failure_at: Optional[float] = None  # when we first saw this fail

    @property
    def pass_rate(self) -> float:
        return self.passed / self.runs if self.runs else 0.0

    @property
    def flakiness(self) -> str:
        """Categorize: reliable / flaky / broken.

        - reliable: 100% pass over ≥2 runs
        - flaky: 1 ≤ passed < runs (sometimes passes, sometimes doesn't)
        - broken: 0 passes despite ≥2 runs
        - new: only 1 run so far (not enough signal)
        """
        if self.runs < 2:
            return "new"
        if self.passed == self.runs:
            return "reliable"
        if self.passed == 0:
            return "broken"
        return "flaky"


@dataclass
class HistoryTrends:
    """Aggregate view across all test sessions in a nation."""

    total_sessions: int
    total_case_runs: int
    overall_pass_rate: float
    by_case: dict[str, CaseStats] = field(default_factory=dict)
    recent_failures: list[tuple[str, str, str]] = field(default_factory=list)
    # ↑ list of (session_id, case_name, error_summary) — most recent first

    @property
    def reliable(self) -> list[CaseStats]:
        return sorted(
            (c for c in self.by_case.values() if c.flakiness == "reliable"),
            key=lambda c: -c.last_seen,
        )

    @property
    def flaky(self) -> list[CaseStats]:
        return sorted(
            (c for c in self.by_case.values() if c.flakiness == "flaky"),
            key=lambda c: c.pass_rate,
        )

    @property
    def broken(self) -> list[CaseStats]:
        return sorted(
            (c for c in self.by_case.values() if c.flakiness == "broken"),
            key=lambda c: -c.last_seen,
        )

    @property
    def fresh(self) -> list[CaseStats]:
        return sorted(
            (c for c in self.by_case.values() if c.flakiness == "new"),
            key=lambda c: -c.last_seen,
        )


def aggregate_trends(
    nation_dir: Path, *, limit: int = 100
) -> HistoryTrends:
    """Walk recent sessions, build cross-case stability stats.

    Matches cases by NAME (since per-session ids are local). A case
    renamed across sessions counts as two different cases — that's
    a deliberate choice: cases SHOULD be stable; renaming them mid-
    project usually means a real change in scope.

    `limit` caps the number of sessions to read; default 100 keeps
    the aggregation fast even with months of history.
    """
    metas = list_sessions(nation_dir, limit=limit)
    trends = HistoryTrends(
        total_sessions=0, total_case_runs=0, overall_pass_rate=0.0,
    )
    total_passes = 0
    recent: list[tuple[str, str, str]] = []
    for m in metas:
        try:
            sess = load_session_json(m.path)
        except Exception:  # noqa: BLE001
            continue
        trends.total_sessions += 1
        for r in sess.results:
            name = r.case.name
            trends.total_case_runs += 1
            if r.status == "passed":
                total_passes += 1
            entry = trends.by_case.get(name)
            if entry is None:
                entry = CaseStats(
                    name=name, runs=0, passed=0,
                    last_status=r.status, last_seen=sess.started_at,
                )
                trends.by_case[name] = entry
            entry.runs += 1
            if r.status == "passed":
                entry.passed += 1
            # Track latest-seen (sessions iterate newest-first so the
            # FIRST observation of each case is the newest one).
            if sess.started_at >= entry.last_seen:
                entry.last_seen = sess.started_at
                entry.last_status = r.status
                entry.last_error = r.error
            if r.status in ("failed", "errored"):
                if entry.first_failure_at is None or sess.started_at < entry.first_failure_at:
                    entry.first_failure_at = sess.started_at
                # Recent failures list — preserve session order.
                recent.append((
                    m.id, name, (r.error or r.status)[:80],
                ))
    trends.overall_pass_rate = (
        total_passes / trends.total_case_runs if trends.total_case_runs else 0.0
    )
    trends.recent_failures = recent[:20]
    return trends


def resolve_session(
    nation_dir: Path,
    selector: Optional[str] = None,
) -> Optional[Path]:
    """Find a session JSON to act on.

      - selector=None → most recent session
      - selector="latest" / "last" → same as None
      - selector=<id> → exact filename stem match
      - selector=<prefix> → match by prefix of id (e.g. "20260524")

    Returns the JSON path, or None if no match.
    """
    d = reports_dir(nation_dir)
    if not d.exists():
        return None
    sessions = sorted(d.glob("*.json"), reverse=True)
    if not sessions:
        return None
    if selector is None or selector.lower() in ("latest", "last", "recent"):
        return sessions[0]
    selector = selector.strip()
    # Exact match first.
    for p in sessions:
        if p.stem == selector:
            return p
    # Prefix match.
    matches = [p for p in sessions if p.stem.startswith(selector)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return matches[0]  # most recent prefix match
    return None
