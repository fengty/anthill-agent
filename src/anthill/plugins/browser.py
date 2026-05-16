"""Browser plugin — render JS pages, click links, take screenshots.

For everything web_fetch can't do: SPA-style sites, login flows, pages
that hide content behind cookie walls, screenshots for visual debugging.

Built on Playwright. Heavy install (browser binaries ~200MB), so the
dependency is OPT-IN:

    pip install 'anthill-agent[browser]'
    playwright install chromium

Two plugins:
    browser_render   Load a URL, return visible text after JS executes
    browser_screenshot  Load a URL, return a PNG path inside the workspace

We use Playwright's async API with a fresh BrowserContext per call:
- no shared cookies between asks (clean state)
- automatic cleanup on exit
- headless by default

For long-running browser sessions (multi-step form fills), a stateful
'browser session' plugin would be a natural follow-up — but that's
v0.2 territory once we see real demand.
"""

from __future__ import annotations

import time
from typing import Any

from anthill.plugins.base import Plugin, PluginResult
from anthill.plugins.filesystem import resolve_in_workspace


def _missing() -> PluginResult:
    return PluginResult(
        output=None,
        ok=False,
        error=(
            "browser plugins need the [browser] extra. Install with:\n"
            "  pip install 'anthill-agent[browser]'\n"
            "  playwright install chromium"
        ),
    )


class BrowserRenderPlugin(Plugin):
    name = "browser_render"
    description = "Load a URL with a real browser and return visible text after JS."

    async def call(
        self,
        *,
        url: str,
        wait_selector: str | None = None,
        timeout: float = 30.0,
        max_chars: int = 8000,
        **_: Any,
    ) -> PluginResult:
        if not url or not url.startswith(("http://", "https://")):
            return PluginResult(output=None, ok=False, error=f"invalid url: {url!r}")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return _missing()

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    await page.goto(url, timeout=int(timeout * 1000), wait_until="networkidle")
                    if wait_selector:
                        await page.wait_for_selector(wait_selector, timeout=int(timeout * 1000))
                    text = await page.evaluate("() => document.body.innerText || ''")
                    title = await page.title()
                    final_url = page.url
                finally:
                    await browser.close()
        except Exception as e:  # noqa: BLE001
            return PluginResult(
                output=None,
                ok=False,
                error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
            )

        truncated = len(text) > max_chars
        return PluginResult(
            output=text[:max_chars],
            metadata={
                "url": final_url,
                "title": title,
                "truncated": truncated,
                "char_count": len(text),
            },
        )


class BrowserScreenshotPlugin(Plugin):
    name = "browser_screenshot"
    description = "Load a URL and save a PNG screenshot inside the workspace."

    async def call(
        self,
        *,
        url: str,
        save_as: str | None = None,
        full_page: bool = True,
        timeout: float = 30.0,
        **_: Any,
    ) -> PluginResult:
        if not url or not url.startswith(("http://", "https://")):
            return PluginResult(output=None, ok=False, error=f"invalid url: {url!r}")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return _missing()

        # Resolve the output path against the workspace.
        if not save_as:
            save_as = f"screenshot-{int(time.time())}.png"
        try:
            abs_path = resolve_in_workspace(save_as)
        except PermissionError as e:
            return PluginResult(output=None, ok=False, error=str(e))

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(viewport={"width": 1280, "height": 800})
                    page = await context.new_page()
                    await page.goto(url, timeout=int(timeout * 1000), wait_until="networkidle")
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(abs_path), full_page=full_page)
                    final_url = page.url
                finally:
                    await browser.close()
        except Exception as e:  # noqa: BLE001
            return PluginResult(
                output=None,
                ok=False,
                error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
            )

        return PluginResult(
            output=str(abs_path),
            metadata={"url": final_url, "full_page": full_page, "bytes": abs_path.stat().st_size},
        )
