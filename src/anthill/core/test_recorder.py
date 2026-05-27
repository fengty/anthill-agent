"""0.2.44 — Record-once, replay-N test case generation.

User pain (real session): "你给我的模板这个事情好复杂."
Writing a CASE_GENERATION YAML by hand means knowing:
  - the admin API for product creation
  - the SQL for permission setup
  - every CSS selector for the UI flow
  - what to verify at each step

That's 30-60 min of expert manual work per case. Wrong direction.

Right direction: RECORD the flow once (drive it yourself in a real
browser); anthill watches and generates the YAML automatically.
Run it back any number of times with different data.

Playwright ships `playwright codegen` for exactly this. anthill
wraps it:
  1. Launch `playwright codegen <URL>` as a subprocess
  2. User drives the browser; codegen writes Python to a temp file
  3. When user closes the codegen window OR types 'done' in REPL,
     we read the captured script
  4. Parse it into a list of actions (goto / click / fill / etc.)
  5. Convert to anthill `[[browser:]]` markers
  6. Detect literal values that look parametrizable (emails, IDs,
     product names) → suggest as `{placeholders}` for --data mode

This module is the parser + converter. The REPL command wraps the
subprocess. Tests run against captured codegen output (fixtures);
no real browser needed for unit tests.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --- recorded action model ------------------------------------------


@dataclass
class RecordedAction:
    """One step extracted from codegen output."""

    kind: str          # goto / click / fill / press / type / select / wait
    selector: str = ""  # locator string (anthill-style, see _convert_locator)
    value: str = ""    # for fill/type/press: the typed value or key
    raw: str = ""      # the original codegen line for debugging


@dataclass
class RecordedFlow:
    """A full recording: ordered actions + heuristic parameter map."""

    actions: list[RecordedAction] = field(default_factory=list)
    suggested_params: dict[str, str] = field(default_factory=dict)
    # name → original literal value. {email} → "admin@x.com"


# --- parsing playwright codegen output ------------------------------


# Codegen emits lines like:
#   page.goto("https://x.com")
#   page.get_by_role("button", name="Save").click()
#   page.get_by_label("Email").fill("admin@x.com")
#   page.get_by_role("textbox", name="Password").fill("secret")
#   page.locator(".product-card").first.click()
#   page.get_by_text("Add to cart").click()
#   page.keyboard.press("Enter")
# We extract these into RecordedAction structs.
_GOTO_RE = re.compile(r'page\.goto\(\s*["\']([^"\']+)["\']\s*\)')
_GET_BY_ROLE_RE = re.compile(
    r'page\.get_by_role\(\s*["\'](\w+)["\']\s*(?:,\s*name=["\']([^"\']+)["\'])?\s*\)'
    r'(?:\.first|\.last|\.nth\(\d+\))?'
    r'\.(\w+)\(\s*(?:["\']([^"\']*)["\']\s*)?\)'
)
_GET_BY_LABEL_RE = re.compile(
    r'page\.get_by_label\(\s*["\']([^"\']+)["\']\s*\)'
    r'(?:\.first|\.last|\.nth\(\d+\))?'
    r'\.(\w+)\(\s*(?:["\']([^"\']*)["\']\s*)?\)'
)
_GET_BY_TEXT_RE = re.compile(
    r'page\.get_by_text\(\s*["\']([^"\']+)["\']\s*\)'
    r'(?:\.first|\.last|\.nth\(\d+\))?'
    r'\.(\w+)\(\s*(?:["\']([^"\']*)["\']\s*)?\)'
)
_GET_BY_PLACEHOLDER_RE = re.compile(
    r'page\.get_by_placeholder\(\s*["\']([^"\']+)["\']\s*\)'
    r'(?:\.first|\.last|\.nth\(\d+\))?'
    r'\.(\w+)\(\s*(?:["\']([^"\']*)["\']\s*)?\)'
)
_LOCATOR_RE = re.compile(
    r'page\.locator\(\s*["\']([^"\']+)["\']\s*\)'
    r'(?:\.first|\.last|\.nth\(\d+\))?'
    r'\.(\w+)\(\s*(?:["\']([^"\']*)["\']\s*)?\)'
)
_KEYBOARD_PRESS_RE = re.compile(
    r'page\.keyboard\.press\(\s*["\']([^"\']+)["\']\s*\)'
)


def parse_codegen_script(script: str) -> RecordedFlow:
    """Parse Playwright codegen Python output into a RecordedFlow.

    Tolerates the various forms codegen emits across versions. Skips
    boilerplate (imports, context.close, etc.) — only meaningful
    user-driven actions become RecordedAction.
    """
    flow = RecordedFlow()
    if not script:
        return flow

    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # page.goto(URL)
        m = _GOTO_RE.search(line)
        if m:
            flow.actions.append(RecordedAction(
                kind="goto", selector="", value=m.group(1), raw=line,
            ))
            continue

        # page.get_by_role("role", name="X").action(value)
        m = _GET_BY_ROLE_RE.search(line)
        if m:
            role, name, action, value = m.group(1), m.group(2) or "", m.group(3), m.group(4) or ""
            selector = _role_to_selector(role, name)
            flow.actions.append(RecordedAction(
                kind=_normalize_action(action), selector=selector,
                value=value, raw=line,
            ))
            continue

        # page.get_by_label("X").action(value)
        m = _GET_BY_LABEL_RE.search(line)
        if m:
            label, action, value = m.group(1), m.group(2), m.group(3) or ""
            selector = f'[aria-label="{label}"]'
            flow.actions.append(RecordedAction(
                kind=_normalize_action(action), selector=selector,
                value=value, raw=line,
            ))
            continue

        # page.get_by_text("X").action(value)
        m = _GET_BY_TEXT_RE.search(line)
        if m:
            text, action, value = m.group(1), m.group(2), m.group(3) or ""
            selector = f'text="{text}"'
            flow.actions.append(RecordedAction(
                kind=_normalize_action(action), selector=selector,
                value=value, raw=line,
            ))
            continue

        # page.get_by_placeholder("X").action(value)
        m = _GET_BY_PLACEHOLDER_RE.search(line)
        if m:
            placeholder, action, value = m.group(1), m.group(2), m.group(3) or ""
            selector = f'[placeholder="{placeholder}"]'
            flow.actions.append(RecordedAction(
                kind=_normalize_action(action), selector=selector,
                value=value, raw=line,
            ))
            continue

        # page.locator("CSS").action(value)
        m = _LOCATOR_RE.search(line)
        if m:
            sel, action, value = m.group(1), m.group(2), m.group(3) or ""
            flow.actions.append(RecordedAction(
                kind=_normalize_action(action), selector=sel,
                value=value, raw=line,
            ))
            continue

        # page.keyboard.press("Enter")
        m = _KEYBOARD_PRESS_RE.search(line)
        if m:
            flow.actions.append(RecordedAction(
                kind="press", selector="body", value=m.group(1), raw=line,
            ))
            continue

    flow.suggested_params = detect_parameters(flow.actions)
    return flow


def _normalize_action(codegen_action: str) -> str:
    """Map codegen action names to anthill browser kinds."""
    # codegen emits .click(), .fill("..."), .check(), .uncheck(),
    # .press("Enter"), .select_option("..."), .type("...")
    mapping = {
        "click": "click",
        "dblclick": "click",
        "fill": "fill",
        "type": "fill",
        "press": "press",
        "check": "click",
        "uncheck": "click",
        "select_option": "fill",
        "hover": "click",
    }
    return mapping.get(codegen_action, codegen_action)


def _role_to_selector(role: str, name: str) -> str:
    """Convert a get_by_role(role, name) to an anthill selector.

    Playwright supports `role=button[name="X"]` natively. anthill's
    browser_action passes selectors straight to page.click / etc.,
    which respects role= syntax."""
    if name:
        # Escape any quotes in the name.
        esc = name.replace('"', '\\"')
        return f'role={role}[name="{esc}"]'
    return f'role={role}'


# --- parameter detection -------------------------------------------


# Heuristics: things that look like emails, integer IDs, product
# names, dates, URLs — likely vary across test runs and deserve a
# {placeholder} for the data table.
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")
_INT_ID_RE = re.compile(r"^\d{3,}$")
_URL_RE = re.compile(r"^https?://")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def detect_parameters(actions: list[RecordedAction]) -> dict[str, str]:
    """Heuristically pick literal values that should become
    parameters in the resulting test template.

    The goal: a recording of "log in as alice@x.com → search 'iPhone
    15' → buy" should generate parameters {email}, {product_name}.
    Then the same template runs against (bob@y.com, "Galaxy S24") etc.

    Strategy: look at fill / type / goto values; classify by shape.
    Name placeholders sensibly using the action / selector context.
    """
    params: dict[str, str] = {}
    fill_count = 0
    for a in actions:
        if not a.value:
            continue
        # URLs from goto.
        if a.kind == "goto" and _URL_RE.match(a.value):
            if "{base_url}" not in params:
                params["base_url"] = a.value
            continue
        if a.kind not in ("fill", "type"):
            continue
        # Email-shaped → {email} (or email2 etc. for additional ones).
        if _EMAIL_RE.match(a.value):
            key = "email" if "email" not in params else f"email{len([k for k in params if k.startswith('email')])}"
            params[key] = a.value
            continue
        # Password-shaped (selector hints password / pwd).
        sel_low = a.selector.lower()
        if "password" in sel_low or "pwd" in sel_low:
            params["password"] = a.value
            continue
        # Numeric IDs / SKUs (3+ digits).
        if _INT_ID_RE.match(a.value):
            key = "id" if "id" not in params else f"id{len([k for k in params if k.startswith('id')])}"
            params[key] = a.value
            continue
        # Dates.
        if _DATE_RE.match(a.value):
            params.setdefault("date", a.value)
            continue
        # Plain string → product_name / search_term / etc.
        # Use selector context to name it.
        fill_count += 1
        name_hint = _guess_param_name(a.selector, fill_count)
        # Avoid clobbering an existing key.
        unique = name_hint
        idx = 2
        while unique in params:
            unique = f"{name_hint}{idx}"
            idx += 1
        params[unique] = a.value
    return params


def _guess_param_name(selector: str, default_idx: int) -> str:
    """Pick a reasonable param name from selector context."""
    s = selector.lower()
    for hint, name in [
        ("name=\"email\"", "email"),
        ("email", "email"),
        ("search", "search_term"),
        ("name=\"product", "product_name"),
        ("product", "product_name"),
        ("price", "price"),
        ("quantity", "quantity"),
        ("qty", "quantity"),
        ("address", "address"),
        ("phone", "phone"),
        ("name=\"name", "name"),
        ("title", "title"),
        ("desc", "description"),
    ]:
        if hint in s:
            return name
    return f"value{default_idx}"


# --- YAML generation -----------------------------------------------


def to_test_case_yaml(
    flow: RecordedFlow,
    *,
    case_name: str = "recorded flow",
    expected: str = "(fill in expected outcome)",
    verification: str = "(fill in how to verify)",
) -> str:
    """Convert a RecordedFlow to anthill's --data YAML format.

    The YAML uses {placeholder} substitution for the values
    `detect_parameters` identified, plus a single `rows:` entry
    with the original values (so the recording replays as-is on
    first run; user adds more rows to scale).
    """
    # Build steps with placeholders substituted in.
    steps: list[str] = []
    # Reverse lookup: literal value → placeholder name (first match wins).
    value_to_param = {v: k for k, v in flow.suggested_params.items()}

    for a in flow.actions:
        marker = _action_to_marker(a, value_to_param)
        if marker:
            steps.append(marker)

    # Build YAML manually to keep it readable + avoid PyYAML dep.
    lines: list[str] = []
    lines.append("template:")
    lines.append(f'  name: "{_yaml_escape(case_name)} ({{scenario}})"')
    lines.append("  prerequisites: ''")
    lines.append("  steps:")
    for s in steps:
        lines.append(f"    - {_yaml_quote(s)}")
    lines.append(f'  expected: "{_yaml_escape(expected)}"')
    lines.append(f'  verification: "{_yaml_escape(verification)}"')
    lines.append("rows:")
    # Row 1: the literal values used during recording.
    lines.append('  - scenario: "原始录制"')
    for k, v in flow.suggested_params.items():
        lines.append(f"    {k}: {_yaml_quote(v)}")
    # Row 2 stub for user to fill in another data point.
    if flow.suggested_params:
        lines.append('  # - scenario: "另一组数据"  # 取消注释 + 改值')
        for k in flow.suggested_params:
            lines.append(f"  #   {k}: REPLACE_ME")
    return "\n".join(lines) + "\n"


def _action_to_marker(
    action: RecordedAction, value_to_param: dict[str, str],
) -> str:
    """Convert one RecordedAction to a [[browser:...]] marker string,
    substituting parameterized values with {placeholder}."""
    val = action.value
    # If this literal was identified as a parameter, swap to placeholder.
    if val and val in value_to_param:
        val = "{" + value_to_param[val] + "}"

    if action.kind == "goto":
        return f"[[browser:goto {val}]]"
    if action.kind == "click":
        return f"[[browser:click {action.selector}]]"
    if action.kind == "fill":
        return f"[[browser:fill {action.selector} {val}]]"
    if action.kind == "press":
        return f"[[browser:press {action.selector} {val}]]"
    # Unknown kind — emit a comment so the user can fix manually.
    return f"# unknown action: {action.raw}"


def _yaml_escape(s: str) -> str:
    """Escape double-quotes in a YAML double-quoted scalar."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _yaml_quote(s: str) -> str:
    """Pick the safest YAML scalar form for `s`. Most strings can
    use plain double-quoted; reserved characters need block style."""
    # If the value has chars YAML interprets specially, wrap in
    # double quotes with escaping. Otherwise plain quote it anyway
    # for consistency.
    return '"' + _yaml_escape(s) + '"'


# --- recorder subprocess wrapper -----------------------------------


def codegen_available() -> bool:
    """True if `playwright codegen` is on PATH."""
    return shutil.which("playwright") is not None


@dataclass
class RecordingResult:
    ok: bool
    script: str = ""
    error: str = ""
    duration_seconds: float = 0.0


def record_with_codegen(
    start_url: str,
    *,
    output_path: Path | None = None,
    timeout: float = 600.0,
) -> RecordingResult:
    """Launch `playwright codegen` and capture the generated script.

    The user drives the browser. When they close the inspector
    window (or hit Ctrl+C in the terminal), codegen writes the
    final script to `output_path` (or stdout).

    Returns RecordingResult with the captured script text. We
    don't parse here — caller pipes the script through
    parse_codegen_script.
    """
    if not codegen_available():
        return RecordingResult(
            ok=False,
            error=(
                "playwright not found on PATH. Install with "
                "/setup browser (or pip install playwright && "
                "playwright install chromium)."
            ),
        )
    if output_path is None:
        output_path = Path("/tmp") / f"anthill-codegen-{int(time.time())}.py"
    # `playwright codegen --output PATH URL` writes the script to
    # PATH and opens the browser + inspector. Blocks until user
    # closes the inspector.
    cmd = ["playwright", "codegen", "--output", str(output_path), start_url]
    started = time.perf_counter()
    try:
        subprocess.run(cmd, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return RecordingResult(
            ok=False,
            error=f"codegen timed out after {timeout}s",
            duration_seconds=time.perf_counter() - started,
        )
    except FileNotFoundError as e:
        return RecordingResult(
            ok=False, error=f"failed to launch codegen: {e}",
            duration_seconds=time.perf_counter() - started,
        )
    duration = time.perf_counter() - started
    if not Path(output_path).exists():
        return RecordingResult(
            ok=False, error="codegen produced no output file",
            duration_seconds=duration,
        )
    script = Path(output_path).read_text(encoding="utf-8")
    return RecordingResult(
        ok=True, script=script, duration_seconds=duration,
    )
