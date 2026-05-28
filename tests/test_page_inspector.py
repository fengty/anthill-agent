"""0.2.48 — direct page inspection without LLM in the loop.

Lesson from 0.2.45 production failure: asking the LLM to "open
URL and report" depends on 3 things going right (model emits tool,
tool runs, model writes report). When ANY of them flakes, you get
89 chars of nothing.

Reliable alternative tested here: anthill itself fetches and
extracts. Tests use a tiny stub HTML to verify the httpx fallback
parses correctly — Playwright tests live elsewhere (need a real
browser).
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, patch

import pytest

from anthill.core.page_inspector import (
    PageContext,
    _collapse_whitespace,
    _inspect_with_httpx,
    inspect_url,
)


# A representative SSR HTML page: login form + nav + buttons + text.
_FIXTURE_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <title>Admin Dashboard | Shop</title>
</head>
<body>
  <nav>
    <a href="/products">Products</a>
    <a href="/orders">Orders</a>
    <a href="/customers">Customers</a>
    <a href="/settings">Settings</a>
  </nav>
  <main>
    <h1>Login Required</h1>
    <form method="post" action="/login">
      <input type="email" name="email" placeholder="Email address" />
      <input type="password" name="password" aria-label="Password" />
      <button type="submit">Sign In</button>
    </form>
    <p>Don't have an account? <a href="/signup">Create one</a></p>
    <button>Cancel</button>
  </main>
</body>
</html>
"""


# --- PageContext rendering ----------------------------------------


def test_render_for_qa_includes_all_sections() -> None:
    """The rendered block has title + forms + buttons + links +
    text — exactly the 5 things case-gen needs."""
    ctx = PageContext(
        url="http://x.com",
        ok=True,
        method="httpx",
        title="My Page",
        text_excerpt="some content here",
        forms=["email: name=\"email\""],
        buttons=["Sign In", "Cancel"],
        links=["Products → /products"],
    )
    rendered = ctx.render_for_qa()
    assert "My Page" in rendered
    assert "email" in rendered
    assert "Sign In" in rendered
    assert "Products" in rendered
    assert "some content here" in rendered
    # Labeled sections so the LLM can parse.
    assert "TITLE:" in rendered
    assert "FORMS" in rendered
    assert "BUTTONS" in rendered


def test_render_for_qa_when_failed() -> None:
    """Failure case: render still produces something, with the
    error message so case-gen knows why context is missing."""
    ctx = PageContext(
        url="http://x.com", ok=False, method="failed",
        error="connection refused",
    )
    rendered = ctx.render_for_qa()
    assert "FAILED" in rendered
    assert "connection refused" in rendered


# --- httpx extraction --------------------------------------------


def test_httpx_extracts_title_forms_buttons_links() -> None:
    """End-to-end on fixture HTML: parse all 4 element categories."""
    import httpx

    # Mock the AsyncClient to return our fixture.
    class _MockResp:
        text = _FIXTURE_HTML
        def raise_for_status(self): pass

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get(self, _url): return _MockResp()

    with patch.object(httpx, "AsyncClient", lambda **kwargs: _MockClient()):
        ctx = asyncio.run(_inspect_with_httpx("http://x.com", timeout=5))

    assert ctx.ok
    assert ctx.method == "httpx"
    # Title.
    assert "Admin Dashboard" in ctx.title
    # Forms: 2 inputs (email + password).
    assert any("email" in f for f in ctx.forms)
    assert any("password" in f for f in ctx.forms)
    # Buttons: Sign In + Cancel.
    assert "Sign In" in ctx.buttons
    assert "Cancel" in ctx.buttons
    # Links: 5 nav links + signup.
    link_texts = [l.split(" →")[0] for l in ctx.links]
    assert "Products" in link_texts
    assert "Orders" in link_texts
    assert "Create one" in link_texts


def test_httpx_handles_network_error() -> None:
    """Connection refused / timeout → PageContext(ok=False) with
    error msg. Don't raise."""
    import httpx

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get(self, _url):
            raise httpx.ConnectError("connection refused")

    with patch.object(httpx, "AsyncClient", lambda **kwargs: _MockClient()):
        ctx = asyncio.run(_inspect_with_httpx("http://x.com", timeout=5))

    assert not ctx.ok
    assert ctx.method == "failed"
    assert "connection refused" in ctx.error.lower()


def test_httpx_strips_html_from_body_text() -> None:
    """Body excerpt should have HTML tags stripped — pure text only.
    The LLM is asked to use this for 'page contains X' assertions;
    HTML noise would confuse it."""
    import httpx

    html = """<html><body><h1>Hello world</h1>
    <p>This is <strong>bold</strong> text.</p>
    <script>alert("x");</script>
    </body></html>"""

    class _MockResp:
        text = html
        def raise_for_status(self): pass

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get(self, _url): return _MockResp()

    with patch.object(httpx, "AsyncClient", lambda **kwargs: _MockClient()):
        ctx = asyncio.run(_inspect_with_httpx("http://x.com", timeout=5))

    assert ctx.ok
    # HTML tags removed.
    assert "<h1>" not in ctx.text_excerpt
    assert "<strong>" not in ctx.text_excerpt
    # But text content survives.
    assert "Hello world" in ctx.text_excerpt
    assert "bold" in ctx.text_excerpt


# --- whitespace collapse --------------------------------------------


def test_collapse_whitespace() -> None:
    """Real HTML has \\n, \\t, multiple spaces. Collapse to single
    spaces so the excerpt is readable."""
    assert _collapse_whitespace("hello   world\n\nfoo\tbar") == "hello world foo bar"
    assert _collapse_whitespace("  trim  ") == "trim"
    assert _collapse_whitespace("") == ""


# --- inspect_url top-level ------------------------------------------


def test_inspect_url_falls_back_to_httpx_when_playwright_fails() -> None:
    """If Playwright isn't installed / can't start, inspect_url
    falls back to httpx automatically. No surprises for the caller."""
    import httpx

    class _MockResp:
        text = _FIXTURE_HTML
        def raise_for_status(self): pass

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get(self, _url): return _MockResp()

    # Patch out Playwright to force the fallback.
    async def _fake_pw_inspect(url, *, timeout):
        return PageContext(
            url=url, ok=False, method="failed", error="playwright unavailable",
        )

    with patch(
        "anthill.core.page_inspector._inspect_with_playwright", _fake_pw_inspect,
    ), patch.object(httpx, "AsyncClient", lambda **kwargs: _MockClient()):
        ctx = asyncio.run(inspect_url("http://x.com"))

    assert ctx.ok
    assert ctx.method == "httpx"
    assert "Admin Dashboard" in ctx.title


def test_inspect_url_returns_failed_when_both_methods_fail() -> None:
    """When everything is broken, return PageContext(ok=False) with
    a usable error message. Caller decides whether to soldier on."""
    import httpx

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get(self, _url):
            raise httpx.ConnectError("nope")

    async def _fake_pw_inspect(url, *, timeout):
        return PageContext(
            url=url, ok=False, method="failed", error="pw down",
        )

    with patch(
        "anthill.core.page_inspector._inspect_with_playwright", _fake_pw_inspect,
    ), patch.object(httpx, "AsyncClient", lambda **kwargs: _MockClient()):
        ctx = asyncio.run(inspect_url("http://x.com"))

    assert not ctx.ok
    assert ctx.error is not None
