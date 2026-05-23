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


def write_report(session: TestSession, nation_dir: Path) -> Path:
    """Write the markdown report and return the path."""
    d = reports_dir(nation_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(session.started_at))
    # Slug from first case name or "test-session".
    if session.cases:
        slug_src = session.cases[0].name
    else:
        slug_src = session.requirement[:40] or "test-session"
    slug = re.sub(r"[^\w一-鿿]+", "-", slug_src).strip("-").lower()[:30]
    path = d / f"{stamp}-{slug or 'session'}.md"
    path.write_text(format_report(session), encoding="utf-8")
    return path
