"""0.2.26 — persistent browser driving for functional UI testing.

The bug we're closing: anthill's `browser_render` plugin fetches a
URL and returns HTML — that's all it does. It can't click a
button, fill a form, screenshot a state, or wait for an element.
A QA tester's workflow ("点击界面") was impossible before this.

This module ships a thin Playwright wrapper with the actions an
LLM-driven QA flow needs:

  goto URL                  navigate to a page
  click SELECTOR            click an element
  fill SELECTOR VALUE       type VALUE into an input
  press SELECTOR KEY        send a key (Enter, Tab, ...)
  text SELECTOR             return the element's text content
  wait SELECTOR [state]     wait until visible/hidden (default visible)
  screenshot [name]         save a PNG; returns its path
  url                       return current URL
  reload                    reload the current page
  evaluate JS               run JS and return its result (advanced)

Design choices:

  - PERSISTENT session per nation. A test flow is multi-step
    (login → navigate → click → assert). Starting a new browser
    per step would lose login state, cookies, JS state.
  - ASYNC throughout. Playwright's async_api is the supported path
    inside asyncio; sync_api would deadlock the REPL.
  - LAZY init. The browser doesn't start until the first [[browser:]]
    block executes — opening the entire Playwright stack costs
    ~600ms on macOS.
  - PERSISTED storage_state. cookies/localStorage survive REPL
    restart so a logged-in test session is reusable next day.
  - DEFENSIVE timeouts (10s per action). UI hangs shouldn't lock
    the REPL.

Error semantics: every action returns a BrowserResult dataclass.
The model sees structured outcomes (ok/error message/value), not
opaque exceptions.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# Hard timeout for a single browser action. Tighter than shell's
# 30s because UI ops should finish fast — if waiting >10s, something
# is wrong with the test setup, not the test command.
DEFAULT_ACTION_TIMEOUT_MS: int = 10_000


@dataclass
class BrowserResult:
    """The outcome of one [[browser:CMD]] action.

    `value` carries any data the action produced (page text, URL,
    screenshot path, evaluate() result). `error` is set on failure
    instead.
    """

    action: str
    args: str
    ok: bool
    value: Any = None
    error: Optional[str] = None
    duration_seconds: float = 0.0

    @property
    def short_summary(self) -> str:
        if not self.ok:
            return f"error: {self.error}"
        if isinstance(self.value, str) and len(self.value) > 80:
            return f"ok — {self.value[:80]}…"
        if self.value is not None:
            return f"ok — {self.value!r}"
        return "ok"


# --- marker parsing ---------------------------------------------------


# Header captures the [[browser:ACTION ] prefix (note the SINGLE ]
# at the end is intentional — we strip past whitespace and then start
# walking the args body bracket-balanced). The closer `]]` is then
# located by a manual walker that respects nested `[...]` in CSS
# selectors so `[[browser:click button[type=submit]]]` parses
# correctly (the inner `]` belongs to the selector, not the closer).
_BROWSER_HEADER_RE = re.compile(
    r"\[\[\s*browser\s*:\s*(?P<action>\w+)\s*",
    re.IGNORECASE,
)


@dataclass
class BrowserBlock:
    """One [[browser:...]] marker found in model output."""

    start: int
    end: int
    action: str
    args: str


def extract_browser_blocks(text: str) -> list[BrowserBlock]:
    """Pull every [[browser:ACTION ARGS]] from `text` in source order.

    Uses a bracket-balanced walker (not pure regex) so CSS selectors
    with `[attr=value]` brackets parse correctly. E.g.

      [[browser:click button[type=submit]]]
                       ^-----selector-----^

    The closer is the FIRST `]]` encountered at bracket-depth 0
    relative to the args body.
    """
    blocks: list[BrowserBlock] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _BROWSER_HEADER_RE.search(text, pos)
        if m is None:
            break
        action = m.group("action").lower()
        if not action:
            pos = m.end()
            continue
        body_start = m.end()
        # Walk the body, tracking `[` `]` depth so a nested ]'s
        # don't close us early.
        depth = 0
        j = body_start
        close_at = -1
        while j < n - 1:
            two = text[j:j + 2]
            if two == "]]" and depth == 0:
                close_at = j
                break
            ch = text[j]
            if ch == "[":
                depth += 1
            elif ch == "]":
                if depth > 0:
                    depth -= 1
                # depth == 0 here is impossible with our guard above
                # (a lone `]` at depth 0 means malformed selector;
                # we treat it as part of args and keep going).
            j += 1
        if close_at < 0:
            # No closer found — give up on this header.
            break
        args = text[body_start:close_at].strip()
        blocks.append(
            BrowserBlock(
                start=m.start(),
                end=close_at + 2,
                action=action,
                args=args,
            )
        )
        pos = close_at + 2
    return blocks


# Kept for backward compatibility — used by _print_final_output to
# strip markers in /noexec mode. A simple regex is fine here since
# we just want to remove the marker shape; bracket balancing is
# only needed for ARG extraction.
_BROWSER_MARKER_RE = re.compile(
    r"\[\[\s*browser\s*:[^\[]*?\]\]",
    re.IGNORECASE | re.DOTALL,
)


# --- session ----------------------------------------------------------


class BrowserSession:
    """A persistent Playwright session held open across [[browser:]] markers.

    Lifecycle:
      sess = BrowserSession(state_dir=...)
      await sess.start()             # opens chromium, restores cookies
      result = await sess.execute("goto", "https://...")
      ...
      await sess.close()             # persists cookies, kills chromium

    The class is async because Playwright's async_api is the safe
    path inside asyncio. The REPL's _handle_ask is already async, so
    integration is direct.
    """

    def __init__(self, *, state_dir: Path | None = None, headless: bool = False) -> None:
        self.state_dir = state_dir
        self.headless = headless
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None

    # -- lifecycle ---

    async def start(self) -> BrowserResult:
        """Open Playwright + chromium. Idempotent — second call is a no-op.

        Restores cookies/localStorage from state_dir/browser_state.json
        if present so a test session that logged in yesterday picks up
        where it left off.
        """
        if self._page is not None:
            return BrowserResult(action="start", args="", ok=True, value="already open")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return BrowserResult(
                action="start",
                args="",
                ok=False,
                error="Playwright not installed. Run /setup browser.",
            )

        started = time.perf_counter()
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
            )
            ctx_kwargs: dict[str, Any] = {}
            state_path = self._state_file()
            if state_path is not None and state_path.exists():
                ctx_kwargs["storage_state"] = str(state_path)
            self._context = await self._browser.new_context(**ctx_kwargs)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(DEFAULT_ACTION_TIMEOUT_MS)
        except Exception as e:  # noqa: BLE001
            return BrowserResult(
                action="start",
                args="",
                ok=False,
                error=f"failed to start: {type(e).__name__}: {e}",
                duration_seconds=time.perf_counter() - started,
            )
        return BrowserResult(
            action="start",
            args="",
            ok=True,
            value=f"chromium up (headless={self.headless})",
            duration_seconds=time.perf_counter() - started,
        )

    async def close(self) -> None:
        """Persist cookies + tear down. Safe to call multiple times."""
        try:
            if self._context is not None:
                state_path = self._state_file()
                if state_path is not None:
                    try:
                        state_path.parent.mkdir(parents=True, exist_ok=True)
                        await self._context.storage_state(path=str(state_path))
                    except Exception:  # noqa: BLE001
                        pass
                await self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def _state_file(self) -> Path | None:
        if self.state_dir is None:
            return None
        return Path(self.state_dir) / "browser_state.json"

    def _screenshot_dir(self) -> Path | None:
        if self.state_dir is None:
            return None
        return Path(self.state_dir) / "screenshots"

    # -- dispatch ---

    async def execute(self, action: str, args: str) -> BrowserResult:
        """Run one action. Auto-starts the session on first call."""
        if self._page is None:
            start_r = await self.start()
            if not start_r.ok:
                return BrowserResult(
                    action=action, args=args, ok=False,
                    error=start_r.error,
                )
        handler = _ACTION_HANDLERS.get(action)
        if handler is None:
            return BrowserResult(
                action=action, args=args, ok=False,
                error=(
                    f"unknown action {action!r}. supported: "
                    + ", ".join(sorted(_ACTION_HANDLERS))
                ),
            )
        started = time.perf_counter()
        try:
            value = await handler(self, args)
        except Exception as e:  # noqa: BLE001 — surface as structured error
            return BrowserResult(
                action=action, args=args, ok=False,
                error=f"{type(e).__name__}: {e}",
                duration_seconds=time.perf_counter() - started,
            )
        return BrowserResult(
            action=action, args=args, ok=True, value=value,
            duration_seconds=time.perf_counter() - started,
        )


# --- action handlers --------------------------------------------------


async def _act_goto(sess: BrowserSession, args: str) -> str:
    url = args.strip()
    if not url:
        raise ValueError("goto requires a URL")
    await sess._page.goto(url)
    return f"loaded {sess._page.url}"


async def _act_click(sess: BrowserSession, args: str) -> str:
    selector = args.strip()
    if not selector:
        raise ValueError("click requires a selector")
    await sess._page.click(selector)
    return f"clicked {selector}"


async def _act_fill(sess: BrowserSession, args: str) -> str:
    # First whitespace separates selector from value. Selectors can
    # be CSS / xpath / text="..." — none contain unescaped spaces in
    # practice, so split-once is the right rule.
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError("fill requires: SELECTOR VALUE")
    selector, value = parts
    await sess._page.fill(selector, value)
    return f"filled {selector} with {len(value)} chars"


async def _act_press(sess: BrowserSession, args: str) -> str:
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError("press requires: SELECTOR KEY")
    selector, key = parts
    await sess._page.press(selector, key)
    return f"pressed {key} on {selector}"


async def _act_text(sess: BrowserSession, args: str) -> str:
    selector = args.strip()
    if not selector:
        # No selector → return the page's whole text body.
        body = await sess._page.text_content("body")
        return body or ""
    txt = await sess._page.text_content(selector)
    return txt or ""


async def _act_wait(sess: BrowserSession, args: str) -> str:
    parts = args.split()
    if not parts:
        raise ValueError("wait requires a selector")
    selector = parts[0]
    state = parts[1] if len(parts) > 1 else "visible"
    if state not in ("visible", "hidden", "attached", "detached"):
        raise ValueError(
            f"wait state must be visible/hidden/attached/detached, got {state!r}"
        )
    await sess._page.wait_for_selector(selector, state=state)
    return f"{selector} → {state}"


async def _act_screenshot(sess: BrowserSession, args: str) -> str:
    name = args.strip() or f"screenshot-{int(time.time())}"
    shots_dir = sess._screenshot_dir()
    if shots_dir is None:
        # Fallback to CWD when no state_dir configured.
        shots_dir = Path.cwd() / ".anthill-screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    # Sanitize the name.
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not safe.endswith(".png"):
        safe += ".png"
    out = shots_dir / safe
    await sess._page.screenshot(path=str(out), full_page=True)
    return str(out)


async def _act_url(sess: BrowserSession, args: str) -> str:
    return sess._page.url


async def _act_reload(sess: BrowserSession, args: str) -> str:
    await sess._page.reload()
    return f"reloaded {sess._page.url}"


async def _act_evaluate(sess: BrowserSession, args: str) -> str:
    if not args.strip():
        raise ValueError("evaluate requires JS expression")
    result = await sess._page.evaluate(args)
    return str(result)


_ACTION_HANDLERS = {
    "goto": _act_goto,
    "click": _act_click,
    "fill": _act_fill,
    "press": _act_press,
    "text": _act_text,
    "wait": _act_wait,
    "screenshot": _act_screenshot,
    "url": _act_url,
    "reload": _act_reload,
    "evaluate": _act_evaluate,
}


def supported_actions() -> tuple[str, ...]:
    """List of supported browser actions — used in the system prompt."""
    return tuple(sorted(_ACTION_HANDLERS))


BROWSER_TOOL_INSTRUCTION = """\
==================
BROWSER TOOL (UI driving):

When the king asks you to actually USE A WEBSITE — click a button,
fill a form, take a screenshot, check what text is on a page — emit
a browser marker:

  [[browser:goto https://example.com/login]]
  [[browser:fill input#email user@example.com]]
  [[browser:fill input#password secret]]
  [[browser:click button[type=submit]]]
  [[browser:wait .dashboard visible]]
  [[browser:text h1.welcome]]
  [[browser:screenshot dashboard-loaded]]

The browser session is PERSISTENT across markers in your response,
AND across multiple asks until the king closes anthill or types
/browser close. Cookies / login state are remembered.

Supported actions:
  goto URL                  navigate
  click SELECTOR            click an element
  fill SELECTOR VALUE       type into an input
  press SELECTOR KEY        send a key (Enter, Tab, ...)
  text [SELECTOR]           get text content (whole body if no selector)
  wait SELECTOR [visible|hidden]  wait for state (default visible)
  screenshot [name]         save a PNG, returns its path
  url                       current URL
  reload                    reload the page
  evaluate JS               run JS, return result

Selectors are CSS by default. text="some text" also works.

This is the tool for FUNCTIONAL TESTING — the kind a human QA
person does. When the king says "test the login flow", chain the
markers to drive the actual UI; don't describe the test in words.
=================="""
