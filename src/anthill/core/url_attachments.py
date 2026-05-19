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
            recovered = await _try_browser_fallback(url, per_url_cap=per_url_cap)
            if recovered is not None:
                if total_so_far + recovered.char_count <= total_cap:
                    total_so_far += recovered.char_count
                    block.fetched.append(recovered)
                    continue
            block.errors.append(
                FetchError(
                    url=url,
                    reason=(
                        "fetched but looks like a login wall — "
                        "paste the content directly if you can "
                        "(or install 'anthill-agent[browser]' for JS pages)"
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
            recovered = await _try_browser_fallback(url, per_url_cap=per_url_cap)
            if recovered is not None:
                if total_so_far + recovered.char_count <= total_cap:
                    total_so_far += recovered.char_count
                    block.fetched.append(recovered)
                    continue
            block.errors.append(
                FetchError(
                    url=url,
                    reason=(
                        f"fetched only {fetched.char_count} chars — "
                        f"looks like a redirect / auth gate / empty "
                        f"response. Paste the content directly "
                        f"(or install 'anthill-agent[browser]' for JS pages)"
                    ),
                )
            )
            continue
        total_so_far += fetched.char_count
        block.fetched.append(fetched)
    return block


async def _try_browser_fallback(
    url: str, *, per_url_cap: int
) -> "FetchedURL | None":
    """0.1.54 — try Playwright when httpx gave us nothing useful.

    Real-user trigger: Zentao bug pages served ~100 bytes to httpx
    (redirect stub / cookie gate). A real browser gets the actual
    bug body because it runs the JS that fills in the page.

    Returns None when:
      - Playwright isn't installed (the [browser] extra opt-in)
      - The browser render also produced nothing useful
      - Any exception during the render (treat as graceful no-op)

    The cost is the price of admission: a Playwright render is
    multiple seconds. We pay it only on the failure branch, never
    on a successful httpx fetch. This is the right tradeoff —
    failing fast on httpx is cheap; succeeding slowly via browser
    on the OTHERWISE-failed cases is a strict UX improvement.
    """
    try:
        from anthill.plugins.browser import BrowserRenderPlugin
    except ImportError:
        return None
    plugin = BrowserRenderPlugin()
    try:
        result = await plugin.call(
            url=url, timeout=20.0, max_chars=per_url_cap
        )
    except Exception:  # noqa: BLE001 — fallback must never crash the fetch path
        return None
    if not result.ok or not result.output:
        return None
    text = str(result.output)
    if len(text) < THIN_CONTENT_THRESHOLD_CHARS:
        # The browser also got nothing real — don't fake it.
        return None
    return FetchedURL(
        url=url,
        display_host=_display_host(url),
        content=text,
        char_count=len(text),
        is_login_wall=False,
        # Mark the source so post-hoc analysis can tell httpx fetches
        # apart from playwright-rescued ones.
        via_browser=True,
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
