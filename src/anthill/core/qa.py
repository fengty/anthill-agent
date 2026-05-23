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
