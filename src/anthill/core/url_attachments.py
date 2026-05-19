"""0.1.38 — URL auto-attachment: paste a URL, get its content fetched.

The exact bug a real user hit:

  » 分析下：http://ss.chandao.pamirs.top/zentao/bug-view-56128.html
  ✗ "I can't access external links. Please paste the content."

We HAVE ``WebFetchPlugin`` but it's a CLI utility — it doesn't fire
during asks because Anthill doesn't ship LLM tool-calling integration
yet. Building that is a multi-week patch.

The pragmatic shortcut, mirroring the 0.1.11 ``@file`` pattern: at
the REPL layer, detect any ``http(s)://...`` token in the input,
fetch it via the existing plugin, and inline the readable text into
the prompt before Scout sees it. Same insertion point, same
attachment-block UI, same caps. No agent-architecture change.

What this catches: pasted URLs in plain text prompts. What it
doesn't: URLs the model "decides" to fetch mid-task (that needs
real tool-calling). Different problem, different patch.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass, field


# URL regex — pragmatically permissive. Catches http(s) URLs with
# common chars; doesn't try to be RFC 3986 perfect. We err on the
# side of detecting more, since the fetch step does its own
# validation and a non-URL string just produces an error we display.
_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
)

# Trailing punctuation to strip — same as for @-file tokens.
_TRAILING_NOISE = ".,;!?。，；！？:：—-—_'\"`「」『』 \t)]}"


def has_url(text: str) -> bool:
    """0.1.53 — fast check used by conversation.is_follow_up and the
    clarifier guard: does ``text`` contain a fetchable URL? URLs are
    fresh-task signals, not follow-up signals — the URL itself is
    the context the user is asking about, so a 2-word "分析下：URL"
    should never inherit prior-turn wrapping.
    """
    return bool(_URL_RE.search(text))

# Per-URL char cap when inlining into the prompt. Web pages can be
# huge; truncating helps both context windows and judge quality.
DEFAULT_PER_URL_CHARS = 8000

# Total cap across all URLs in a single ask.
DEFAULT_TOTAL_CHARS = 30000

# Heuristic: if the fetched text contains these markers, treat the
# response as "this is a login wall, the real content isn't here."
# Skips wasting tokens on an HTML login form.
_LOGIN_WALL_MARKERS = (
    "login", "log in", "sign in", "signin",
    "请登录", "请登陆", "用户登录", "登录页", "登录注册",
    "session expired", "unauthorized", "您没有权限", "未授权",
    "auth required", "authentication required",
    "禅道", "zentao",   # 0.1.39: real-user case — Zentao redirects to /user-login
    "csrf", "_csrf",
)

# 0.1.39 — "thin content" trip. The real-user Zentao URL returned
# ~100 bytes (probably a meta-refresh redirect to /login). That's
# below ANY real bug-tracker page. If the stripped text is under
# this threshold AND we expected substantive content, treat as
# "fetched but unusable" — show the user, don't feed Scout
# garbage. 500 chars covers the common "minimal redirect HTML" /
# "empty error JSON" / "captcha gate" cases without false-flagging
# genuinely brief but useful pages (which are rare on the modern web).
THIN_CONTENT_THRESHOLD_CHARS = 500


@dataclass
class FetchedURL:
    """One successfully-fetched URL."""

    url: str
    display_host: str   # for the UI line ("chandao.pamirs.top")
    content: str
    char_count: int
    is_login_wall: bool = False
    # 0.1.54 — True when the content came from Playwright fallback,
    # not the primary httpx fetch. Lets the REPL show a "via 🌐
    # browser" tag so the user knows why this URL took longer.
    via_browser: bool = False


@dataclass
class FetchError:
    """One URL we couldn't usefully bring in."""

    url: str
    reason: str          # human-readable; "auth wall" / "timeout" / "404" / ...


@dataclass
class URLAttachmentBlock:
    """Result of expanding URL tokens. Empty when no URLs found."""

    fetched: list[FetchedURL] = field(default_factory=list)
    errors: list[FetchError] = field(default_factory=list)
    truncated: bool = False

    def render(self) -> str:
        """Markdown block to inject into the prompt. Empty when no
        successful fetches — callers can prepend unconditionally."""
        if not self.fetched:
            return ""
        parts = ["[fetched URLs — read these before answering]\n"]
        for f in self.fetched:
            parts.append(
                f"<url href={f.url!r}>\n{f.content}\n</url>\n"
            )
        return "".join(parts) + "\n"


def parse_urls(text: str) -> list[str]:
    """Extract every http(s)://... token from ``text``.

    Trims trailing punctuation in the same shape as @file token
    parsing, so "see http://x.com/y." doesn't capture the period.
    Order preserved; duplicates removed (same URL pasted twice → one
    fetch).
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.finditer(text):
        raw = match.group(0)
        while raw and raw[-1] in _TRAILING_NOISE:
            raw = raw[:-1]
        if not raw or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def _display_host(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc or url
    except ValueError:
        return url


def _looks_like_login_wall(text: str) -> bool:
    """Heuristic: does the fetched body smell like an auth gate?

    Looks at the FIRST 2K chars only — login pages put their forms
    near the top. False positives are fine (we still inject, just
    label it); false negatives mean we feed Scout a login HTML form
    as if it were content, which is what we want to avoid.
    """
    head = text[:2000].lower()
    if not head:
        return False
    # Need at least 2 distinct markers to count, so a single
    # incidental "login" word doesn't trip it.
    hits = sum(1 for m in _LOGIN_WALL_MARKERS if m in head)
    return hits >= 2


async def _fetch_one(url: str, *, per_url_cap: int):  # noqa: ANN202
    """Async fetch via WebFetchPlugin. Returns (FetchedURL | None,
    FetchError | None) — exactly one is non-None.

    Import inside the function so the rest of the module loads
    without httpx (relevant for headless tests that don't touch this).
    """
    try:
        from anthill.plugins.web import WebFetchPlugin
    except Exception as exc:  # noqa: BLE001 — best-effort
        return None, FetchError(url=url, reason=f"plugin unavailable: {exc}")
    plugin = WebFetchPlugin()
    result = await plugin.call(url=url, max_chars=per_url_cap)
    if not result.ok:
        return None, FetchError(url=url, reason=str(result.error or "fetch failed"))
    text = str(result.output or "")
    if not text.strip():
        return None, FetchError(url=url, reason="empty response")
    is_wall = _looks_like_login_wall(text)
    return (
        FetchedURL(
            url=url,
            display_host=_display_host(url),
            content=text,
            char_count=len(text),
            is_login_wall=is_wall,
        ),
        None,
    )


async def expand_urls_async(
    text: str,
    *,
    per_url_cap: int = DEFAULT_PER_URL_CHARS,
    total_cap: int = DEFAULT_TOTAL_CHARS,
) -> URLAttachmentBlock:
    """Find every URL in ``text``, fetch them concurrently, return
    a renderable attachment block."""
    urls = parse_urls(text)
    block = URLAttachmentBlock()
    if not urls:
        return block

    tasks = [_fetch_one(u, per_url_cap=per_url_cap) for u in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total_so_far = 0
    for url, res in zip(urls, results):
        if isinstance(res, Exception):
            block.errors.append(FetchError(url=url, reason=f"{type(res).__name__}: {res}"))
            continue
        fetched, err = res  # type: ignore[assignment]
        if err is not None:
            block.errors.append(err)
            continue
        assert fetched is not None
        if total_so_far + fetched.char_count > total_cap:
            block.truncated = True
            block.errors.append(
                FetchError(
                    url=url,
                    reason=(
                        f"skipped — total URL fetch cap ({total_cap} chars) "
                        f"would be exceeded"
                    ),
                )
            )
            break
        # Login walls get demoted: kept in errors (so the user sees
        # the warning) rather than fed into Scout's prompt.
        # 0.1.54 — but before giving up, try the browser plugin as
        # a fallback. Playwright renders JS / sets a real UA / passes
        # the kinds of trivial cookie gates that httpx tripped over.
        # `_try_browser_fallback` returns the same FetchedURL shape
        # if it recovered real content, None otherwise.
        if fetched.is_login_wall:
            outcome = await _try_browser_fallback(url, per_url_cap=per_url_cap)
            # 0.1.71 — if browser ALSO hit a login wall, escalate to
            # the credentialed fallback. Asks the user for nothing here
            # (REPL handles prompt-then-retry); this path runs when
            # creds are already stored.
            if (
                outcome.result is None
                and outcome.why_failed == "browser-still-login-wall"
            ):
                outcome = await _try_browser_with_login(
                    url, per_url_cap=per_url_cap
                )
            if outcome.result is not None:
                if total_so_far + outcome.result.char_count <= total_cap:
                    total_so_far += outcome.result.char_count
                    block.fetched.append(outcome.result)
                    continue
            block.errors.append(
                FetchError(
                    url=url,
                    reason=_render_fallback_failure(
                        primary="fetched but looks like a login wall",
                        browser_failure=outcome.why_failed,
                    ),
                )
            )
            continue
        # 0.1.39 — thin-content demotion. If the response is too
        # short to plausibly be the page the user wanted, treat it
        # the same way as a login wall. Avoids the real-user case
        # where Zentao returned ~100 bytes (a redirect stub) and
        # we'd happily inline it as if it were a bug report.
        # 0.1.54 — same browser fallback as the login-wall branch.
        if fetched.char_count < THIN_CONTENT_THRESHOLD_CHARS:
            outcome = await _try_browser_fallback(url, per_url_cap=per_url_cap)
            if outcome.result is not None:
                if total_so_far + outcome.result.char_count <= total_cap:
                    total_so_far += outcome.result.char_count
                    block.fetched.append(outcome.result)
                    continue
            block.errors.append(
                FetchError(
                    url=url,
                    reason=_render_fallback_failure(
                        primary=(
                            f"fetched only {fetched.char_count} chars — "
                            f"looks like a redirect / auth gate / empty response"
                        ),
                        browser_failure=outcome.why_failed,
                    ),
                )
            )
            continue
        total_so_far += fetched.char_count
        block.fetched.append(fetched)
    return block


def _render_fallback_failure(*, primary: str, browser_failure: str | None) -> str:
    """0.1.70 — assemble the user-visible error string. Includes WHY
    the browser fallback didn't recover so the user can act on it:
      - browser-not-installed → tell them to run /setup browser
      - browser-still-login-wall → tell them the page needs auth
      - browser-render-failed → surface Playwright's error text
      - else → the original "paste content" hint
    """
    if browser_failure is None:
        # Shouldn't happen (caller always provides) — keep the old
        # generic shape for safety.
        return f"{primary}. Paste the content directly if you can."

    if browser_failure == "browser-not-installed":
        return (
            f"{primary}. Browser fallback unavailable: Playwright not "
            f"installed. Run [cyan]/setup browser[/cyan] then retry."
        )
    if browser_failure == "browser-still-login-wall":
        return (
            f"{primary}. Browser fallback also hit a login wall — "
            f"this page needs auth cookies anthill doesn't have. "
            f"Paste the content directly."
        )
    if browser_failure == "browser-empty-output":
        return (
            f"{primary}. Browser fallback ran but the page rendered "
            f"with no visible text (heavy JS / canvas / 404?). "
            f"Paste the content directly."
        )
    if browser_failure == "browser-content-too-short":
        return (
            f"{primary}. Browser fallback returned <50 chars — "
            f"likely an error stub. Paste the content directly."
        )
    if browser_failure.startswith("browser-render-failed"):
        # Strip the "browser-render-failed: " prefix for readability.
        detail = browser_failure.split(":", 1)[-1].strip()
        return (
            f"{primary}. Browser fallback errored: {detail}. "
            f"Paste the content directly."
        )
    # 0.1.71 — login fallback codes.
    if browser_failure == "login-no-creds":
        return (
            f"{primary}. This page needs login — run "
            f"[cyan]/auth add[/cyan] to store credentials for the "
            f"domain, then retry."
        )
    if browser_failure == "login-bad-url":
        return (
            f"{primary}. Couldn't parse the URL's domain to look up "
            f"stored credentials. Paste the content directly."
        )
    if browser_failure.startswith("login-failed"):
        detail = browser_failure.split(":", 1)[-1].strip()
        return (
            f"{primary}. Tried logging in with stored credentials: "
            f"{detail}. Update with [cyan]/auth add[/cyan] or paste "
            f"the content directly."
        )
    if browser_failure == "login-empty-output":
        return (
            f"{primary}. Login succeeded but post-login page rendered "
            f"with no visible text. Paste the content directly."
        )
    # Unknown failure mode — surface verbatim.
    return f"{primary}. Browser fallback: {browser_failure}."


# 0.1.70 — minimum useful length for a browser-rendered page. Much
# lower than THIN_CONTENT_THRESHOLD_CHARS (500) because browser-
# rendered `innerText` is just the VISIBLE content of the page,
# which can legitimately be ~100 chars for a Q&A snippet, a simple
# definition page, or any minimalist landing page. The 500-char
# threshold made sense for raw HTML over httpx (where short = stub
# / redirect) but is wrong here — we'd reject legitimate browser
# output. Use the is_login_wall heuristic instead for quality signal.
_BROWSER_MIN_USEFUL_CHARS = 50


@dataclass
class _BrowserFallbackOutcome:
    """0.1.70 — diagnostic detail surfaced to the caller so the user
    can see WHY the fallback didn't recover (vs the old opaque
    'looks like a login wall' that covered both failure modes)."""

    result: "FetchedURL | None"
    why_failed: str | None  # None when result is not None


async def _try_browser_with_login(
    url: str, *, per_url_cap: int
) -> _BrowserFallbackOutcome:
    """0.1.71 — second-chance fallback that USES stored credentials.

    Called when `_try_browser_fallback` returned
    'browser-still-login-wall' AND we have a `DomainCredentials` for
    the URL's domain in secrets.toml. Performs the login flow via
    Playwright then fetches the original URL.

    Same outcome shape as `_try_browser_fallback`. Failure surfaced
    as 'login-failed: <reason>' so the user can act (wrong password
    → re-add creds; form layout changed → set explicit selectors).
    """
    try:
        from anthill.core.url_credentials import extract_domain, load_credentials
    except ImportError:
        return _BrowserFallbackOutcome(None, "login-creds-module-missing")
    domain = extract_domain(url)
    if not domain:
        return _BrowserFallbackOutcome(None, "login-bad-url")
    creds = load_credentials(domain)
    if creds is None:
        return _BrowserFallbackOutcome(None, "login-no-creds")
    try:
        from anthill.plugins.browser import BrowserRenderPlugin
    except ImportError:
        return _BrowserFallbackOutcome(None, "browser-not-installed")
    plugin = BrowserRenderPlugin()
    try:
        result = await plugin.call_with_login(
            url=url,
            username=creds.username,
            password=creds.password,
            login_url=creds.login_url,
            username_selector=creds.username_selector,
            password_selector=creds.password_selector,
            submit_selector=creds.submit_selector,
            timeout=30.0,
            max_chars=per_url_cap,
        )
    except Exception as e:  # noqa: BLE001
        return _BrowserFallbackOutcome(
            None, f"login-failed: {type(e).__name__}: {e}"
        )
    if not result.ok:
        return _BrowserFallbackOutcome(
            None, f"login-failed: {result.error or 'ok=False'}"
        )
    text = str(result.output or "")
    if _looks_like_login_wall(text):
        # Login completed but still on login page → credentials wrong
        # OR the form went through but expired immediately (rare).
        return _BrowserFallbackOutcome(
            None, "login-failed: post-login page still looks like login wall"
        )
    if len(text) < _BROWSER_MIN_USEFUL_CHARS:
        return _BrowserFallbackOutcome(None, "login-empty-output")
    return _BrowserFallbackOutcome(
        FetchedURL(
            url=url,
            display_host=_display_host(url),
            content=text,
            char_count=len(text),
            is_login_wall=False,
            via_browser=True,
        ),
        None,
    )


async def _try_browser_fallback(
    url: str, *, per_url_cap: int
) -> _BrowserFallbackOutcome:
    """0.1.54 — try Playwright when httpx gave us nothing useful.
    0.1.70 — return reason string so the error message can be useful.

    Real-user trigger: Zentao bug pages served ~100 bytes to httpx
    (redirect stub / cookie gate). A real browser sees the actual
    bug body because it runs the JS that fills in the page.

    Outcome shapes:
      result != None       → recovered, caller should use it
      result is None, why_failed = "browser-not-installed"
      result is None, why_failed = "browser-render-failed: <err>"
      result is None, why_failed = "browser-empty-output"
      result is None, why_failed = "browser-still-login-wall"
      result is None, why_failed = "browser-content-too-short"

    The cost is the price of admission: a Playwright render is
    multiple seconds. We pay it only on the failure branch, never
    on a successful httpx fetch.
    """
    try:
        from anthill.plugins.browser import BrowserRenderPlugin
    except ImportError:
        return _BrowserFallbackOutcome(None, "browser-not-installed")
    plugin = BrowserRenderPlugin()
    try:
        result = await plugin.call(
            url=url, timeout=20.0, max_chars=per_url_cap
        )
    except Exception as e:  # noqa: BLE001 — fallback must never crash the fetch path
        return _BrowserFallbackOutcome(
            None, f"browser-render-failed: {type(e).__name__}: {e}"
        )
    if not result.ok:
        # Most common case: Playwright not installed. The plugin's
        # _missing() returns ok=False with an install-hint error
        # string. Surface as "browser-not-installed" so the user-
        # visible message points at /setup browser specifically.
        err_text = (result.error or "").lower()
        if "[browser]" in err_text or "playwright" in err_text:
            return _BrowserFallbackOutcome(None, "browser-not-installed")
        return _BrowserFallbackOutcome(
            None, f"browser-render-failed: {result.error or 'ok=False'}"
        )
    if not result.output:
        return _BrowserFallbackOutcome(None, "browser-empty-output")
    text = str(result.output)
    # 0.1.70 — if browser also sees a login wall, fail informatively
    # rather than smuggling the login page into the prompt.
    if _looks_like_login_wall(text):
        return _BrowserFallbackOutcome(None, "browser-still-login-wall")
    # 0.1.70 — drop the 500-char threshold here; a real page with
    # short body (e.g. example.com = 129 chars) is still useful.
    # Only reject *catastrophically* short text — the kind that's
    # almost certainly an error stub.
    if len(text) < _BROWSER_MIN_USEFUL_CHARS:
        return _BrowserFallbackOutcome(None, "browser-content-too-short")
    return _BrowserFallbackOutcome(
        FetchedURL(
            url=url,
            display_host=_display_host(url),
            content=text,
            char_count=len(text),
            is_login_wall=False,
            via_browser=True,
        ),
        None,
    )


def expand_urls(text: str, **kwargs) -> URLAttachmentBlock:
    """Sync wrapper. Runs the async fetcher in a fresh event loop;
    safe to call from `_handle_ask` which is itself called via
    `asyncio.run` — we use ``asyncio.get_event_loop`` checks to
    avoid nested-loop errors."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Caller is already inside an event loop — schedule on it.
            # In Anthill the REPL ask path uses asyncio.run, which
            # creates a new loop; we hit this branch from inside
            # _handle_ask which IS running. Use a fresh loop in
            # a thread to avoid 'cannot be called from a running loop'.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run, expand_urls_async(text, **kwargs)
                )
                return future.result()
    except RuntimeError:
        pass
    return asyncio.run(expand_urls_async(text, **kwargs))
