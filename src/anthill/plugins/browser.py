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
        storage_state: dict | None = None,
        **_: Any,
    ) -> PluginResult:
        """Fetch a URL with a real browser.

        0.1.72 — `storage_state` (Playwright dict shape: cookies +
        origins) lets the caller seed the browser with a previously-
        saved logged-in session. When the saved cookies still work,
        the page renders directly and we skip the slow login dance.
        """
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
                    if storage_state is not None:
                        context = await browser.new_context(
                            storage_state=storage_state
                        )
                    else:
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

    async def call_with_login(
        self,
        *,
        url: str,
        username: str,
        password: str,
        login_url: str | None = None,
        username_selector: str | None = None,
        password_selector: str | None = None,
        submit_selector: str | None = None,
        timeout: float = 30.0,
        max_chars: int = 8000,
        wait_after_login_ms: int = 2000,
        **_: Any,
    ) -> PluginResult:
        """0.1.71 — perform login then fetch.

        Flow:
          1. Open a fresh browser context (no shared cookies).
          2. Navigate to login_url (or url if None, letting the server
             redirect us to its login page).
          3. Fill username field + password field. Selectors are
             explicit when provided, else auto-detected:
               username → input[name=account|username|email|user|login]
               password → input[type=password]
               submit   → input[type=submit] OR button[type=submit] OR
                          the form's first button
          4. Submit. Wait `wait_after_login_ms` for the post-login
             navigation/JS settle.
          5. Navigate to the original `url` (skipped if it's same as
             login_url — some sites land you on the dashboard after
             login and we want THAT content).
          6. Extract `document.body.innerText`.

        Defensive about every step — any failure returns ok=False
        with a useful error string. The fallback chain in
        url_attachments.py catches that.
        """
        if not url or not url.startswith(("http://", "https://")):
            return PluginResult(output=None, ok=False, error=f"invalid url: {url!r}")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return _missing()

        # Common username-field name attributes seen across Zentao,
        # Jira, Confluence, GitLab, generic apps. Pulled from looking
        # at real login form HTML.
        _USER_FIELDS = ["account", "username", "email", "user", "login", "loginId"]
        # Password input: `input[type=password]` is universal.
        _PASS_SELECTOR = "input[type=password]"

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context()
                    page = await context.new_page()
                    # Step 1: get to the login form.
                    target_login = login_url or url
                    await page.goto(
                        target_login,
                        timeout=int(timeout * 1000),
                        wait_until="networkidle",
                    )

                    # Step 2: fill username (explicit selector or
                    # auto-detect via field name list).
                    if username_selector:
                        await page.fill(username_selector, username)
                    else:
                        filled = False
                        for name in _USER_FIELDS:
                            sel = f"input[name='{name}']"
                            if await page.query_selector(sel) is not None:
                                await page.fill(sel, username)
                                filled = True
                                break
                        if not filled:
                            return PluginResult(
                                output=None, ok=False,
                                error=(
                                    "login: couldn't locate username field "
                                    "(tried name in: account/username/email/user/login). "
                                    "Pass username_selector explicitly."
                                ),
                            )

                    # Step 3: fill password.
                    pass_sel = password_selector or _PASS_SELECTOR
                    if await page.query_selector(pass_sel) is None:
                        return PluginResult(
                            output=None, ok=False,
                            error=(
                                "login: couldn't locate password field. "
                                "Pass password_selector explicitly."
                            ),
                        )
                    await page.fill(pass_sel, password)

                    # Step 4: submit. Try explicit selector, then
                    # type=submit, then any button inside the form
                    # containing the password field.
                    if submit_selector:
                        await page.click(submit_selector)
                    else:
                        clicked = False
                        for sel in (
                            "input[type=submit]",
                            "button[type=submit]",
                            "form button",
                        ):
                            if await page.query_selector(sel) is not None:
                                await page.click(sel)
                                clicked = True
                                break
                        if not clicked:
                            # Last resort: press Enter on the password field.
                            await page.press(pass_sel, "Enter")

                    # Step 5: wait for login navigation + JS settle.
                    try:
                        await page.wait_for_load_state(
                            "networkidle",
                            timeout=int(timeout * 1000),
                        )
                    except Exception:  # noqa: BLE001
                        # networkidle can be flaky if the server uses
                        # long-polling; fall back to a fixed wait.
                        pass
                    await page.wait_for_timeout(wait_after_login_ms)

                    # Step 6: if `url` differs from `login_url`,
                    # navigate to the target now that we have a session.
                    if login_url and url != login_url:
                        await page.goto(
                            url,
                            timeout=int(timeout * 1000),
                            wait_until="networkidle",
                        )

                    text = await page.evaluate("() => document.body.innerText || ''")
                    title = await page.title()
                    final_url = page.url
                    # 0.1.72 — capture cookie state BEFORE closing the
                    # context. Caller persists this; next fetch on the
                    # same domain skips the login dance entirely.
                    captured_state = await context.storage_state()
                finally:
                    await browser.close()
        except Exception as e:  # noqa: BLE001
            return PluginResult(
                output=None, ok=False,
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
                "via_login": True,
                # storage_state surfaces so url_attachments.py can
                # save_cookie_state(domain, state) after success.
                "storage_state": captured_state,
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
