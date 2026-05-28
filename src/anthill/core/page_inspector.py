"""0.2.48 — Direct page inspection without an LLM in the loop.

Lesson from 0.2.45 production failure: asking the LLM to "open the
URL and report" depends on 3 things going right:
  1. Model emits the right tool call
  2. The tool actually runs
  3. Model writes a useful report from the result

Real session: any one of those can fail, and even when they
"succeed" the inspection comes back as 89 chars of nothing useful.

Reliable alternative: ANTHILL ITSELF opens the URL. Pure
Playwright + httpx code, no model, no agent loop. Captures the
five things QA case generation actually needs:

  - title (for "should display X" assertions)
  - visible text excerpt (for "page contains 'Y'" verification)
  - forms (input names → guides login / search / CRUD test cases)
  - buttons / CTAs (clickable surface → "user clicks Z" scenarios)
  - links (navigation map → "user navigates to A/B/C" cases)

This struct is then prepended to the case-generation prompt. The
LLM has ground truth to work with.

Order of attempts:
  1. Playwright (handles SPAs, client-rendered apps)
  2. httpx + BeautifulSoup-style HTML extraction (lighter, faster)
  3. httpx raw HTML pattern matching (last resort)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PageContext:
    """What we know about a page after direct inspection."""

    url: str
    ok: bool                              # at least one method worked
    method: str = ""                       # 'playwright' / 'httpx' / 'failed'
    title: str = ""
    text_excerpt: str = ""                # first ~2000 chars of body text
    forms: list[str] = field(default_factory=list)    # input/textarea names+labels
    buttons: list[str] = field(default_factory=list)  # button text labels
    links: list[str] = field(default_factory=list)    # nav-link texts (top N)
    error: Optional[str] = None
    duration_seconds: float = 0.0

    def render_for_qa(self) -> str:
        """Compact text block prepended to the case-gen prompt.

        Format the LLM finds easy to act on: clearly labeled
        sections, name actual UI labels seen on the page so the
        model can use them in test steps.
        """
        if not self.ok:
            return (
                f"=== PAGE INSPECTION ({self.url}) ===\n"
                f"FAILED to load: {self.error or 'unknown error'}\n"
                f"=== END ===\n"
            )
        parts = [
            f"=== PAGE INSPECTION ({self.url}) ===",
            f"TITLE: {self.title or '(empty)'}",
        ]
        if self.forms:
            parts.append("FORMS / INPUTS:")
            for f in self.forms[:15]:
                parts.append(f"  - {f}")
        if self.buttons:
            parts.append("BUTTONS / CTAs:")
            for b in self.buttons[:15]:
                parts.append(f"  - {b}")
        if self.links:
            parts.append("LINKS (sample):")
            for l in self.links[:10]:
                parts.append(f"  - {l}")
        if self.text_excerpt:
            parts.append("VISIBLE TEXT (excerpt):")
            parts.append(self.text_excerpt[:1500])
        parts.append("=== END INSPECTION ===")
        return "\n".join(parts)


# --- public entry ---------------------------------------------------


async def inspect_url(
    url: str, *, prefer_playwright: bool = True, timeout: float = 12.0,
) -> PageContext:
    """Open the URL with whatever works; return what we found.

    Tries Playwright first (handles JS-rendered SPAs the user
    typically wants to test). Falls back to httpx for static pages
    and as a last resort when Playwright isn't installed.
    """
    started = time.perf_counter()

    if prefer_playwright:
        result = await _inspect_with_playwright(url, timeout=timeout)
        if result.ok:
            result.duration_seconds = time.perf_counter() - started
            return result

    result = await _inspect_with_httpx(url, timeout=timeout)
    result.duration_seconds = time.perf_counter() - started
    return result


# --- Playwright path ----------------------------------------------


async def _inspect_with_playwright(url: str, *, timeout: float) -> PageContext:
    """Use the existing BrowserSession (0.2.26+) to inspect the page.

    We open our own short-lived session here — no need to share the
    user's persistent browser, this is a one-shot fetch.
    """
    try:
        from anthill.core.browser_drive import BrowserSession
    except ImportError:
        return PageContext(url=url, ok=False, method="failed",
                          error="browser_drive import failed")

    sess = BrowserSession(state_dir=None, headless=True)
    start = await sess.start()
    if not start.ok:
        return PageContext(
            url=url, ok=False, method="failed",
            error=start.error or "playwright unavailable",
        )

    try:
        # Use Playwright's timeout (ms) — short for speed.
        sess._page.set_default_timeout(int(timeout * 1000))
        await sess._page.goto(url, wait_until="domcontentloaded")
        title = await sess._page.title() or ""

        # Visible text from body.
        body_text = ""
        try:
            body_text = await sess._page.text_content("body") or ""
        except Exception:  # noqa: BLE001
            body_text = ""
        body_text = _collapse_whitespace(body_text)[:2000]

        # Enumerate forms / buttons / links via JS evaluate.
        forms = await _eval_safe(
            sess._page,
            """
            Array.from(document.querySelectorAll('input,textarea,select'))
              .slice(0, 30)
              .map(el => {
                const name = el.getAttribute('name') || el.getAttribute('id') || '';
                const label = el.getAttribute('aria-label') ||
                              el.getAttribute('placeholder') || '';
                const type = el.type || el.tagName.toLowerCase();
                return `${type}: name="${name}" label="${label}"`;
              })
            """,
        ) or []

        buttons = await _eval_safe(
            sess._page,
            """
            Array.from(document.querySelectorAll(
              'button, a[role="button"], input[type="submit"], input[type="button"]'
            ))
              .slice(0, 30)
              .map(b => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim())
              .filter(t => t.length > 0 && t.length < 80)
            """,
        ) or []

        links = await _eval_safe(
            sess._page,
            """
            Array.from(document.querySelectorAll('a[href]'))
              .slice(0, 30)
              .map(a => {
                const t = (a.innerText || '').trim();
                const h = a.getAttribute('href') || '';
                return t ? `${t} → ${h}` : '';
              })
              .filter(s => s && s.length < 100)
            """,
        ) or []

        return PageContext(
            url=url, ok=True, method="playwright",
            title=title, text_excerpt=body_text,
            forms=forms, buttons=buttons, links=links,
        )
    except Exception as e:  # noqa: BLE001
        return PageContext(
            url=url, ok=False, method="failed",
            error=f"playwright: {type(e).__name__}: {e}",
        )
    finally:
        try:
            await sess.close()
        except Exception:  # noqa: BLE001
            pass


async def _eval_safe(page, js: str) -> list:
    """page.evaluate that returns [] on any failure."""
    try:
        result = await page.evaluate(js)
        if isinstance(result, list):
            return [str(x) for x in result]
    except Exception:  # noqa: BLE001
        pass
    return []


# --- httpx fallback ----------------------------------------------


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_FORM_INPUT_RE = re.compile(
    r'<(?:input|textarea|select)\b([^>]*)>', re.IGNORECASE,
)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*["\']([^"\']*)["\']')
_BUTTON_RE = re.compile(
    r'<button[^>]*>(.*?)</button>', re.IGNORECASE | re.DOTALL,
)
_LINK_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


async def _inspect_with_httpx(url: str, *, timeout: float) -> PageContext:
    """Static HTML fetch + regex extraction. Doesn't handle SPAs
    but works on most server-rendered apps and is dependency-free."""
    try:
        import httpx
    except ImportError:
        return PageContext(url=url, ok=False, method="failed",
                          error="httpx not installed")

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:  # noqa: BLE001
        return PageContext(
            url=url, ok=False, method="failed",
            error=f"httpx: {type(e).__name__}: {e}",
        )

    # Title.
    title_m = _TITLE_RE.search(html)
    title = _collapse_whitespace(title_m.group(1)) if title_m else ""

    # Body text (strip all HTML).
    body_text = _TAG_RE.sub(" ", html)
    body_text = _collapse_whitespace(body_text)[:2000]

    # Forms.
    forms = []
    for attrs_str in _FORM_INPUT_RE.findall(html)[:30]:
        attrs = dict(_ATTR_RE.findall(attrs_str))
        name = attrs.get("name") or attrs.get("id") or ""
        label = attrs.get("aria-label") or attrs.get("placeholder") or ""
        ftype = attrs.get("type", "text")
        forms.append(f'{ftype}: name="{name}" label="{label}"')

    # Buttons.
    buttons = [
        _collapse_whitespace(_TAG_RE.sub("", b)).strip()
        for b in _BUTTON_RE.findall(html)[:30]
    ]
    buttons = [b for b in buttons if 0 < len(b) < 80]

    # Links.
    links = []
    for href, text in _LINK_RE.findall(html)[:30]:
        t = _collapse_whitespace(_TAG_RE.sub("", text)).strip()
        if t and len(t) < 80:
            links.append(f"{t} → {href}")

    return PageContext(
        url=url, ok=True, method="httpx",
        title=title, text_excerpt=body_text,
        forms=forms, buttons=buttons, links=links,
    )


def _collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace into single spaces, trim."""
    return " ".join(text.split())
