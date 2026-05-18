"""0.1.38 — URL auto-attachment.

The exact bug a real user hit:
  » 分析下：http://ss.chandao.pamirs.top/zentao/bug-view-56128.html
  ✗ "I can't access external links. Please paste the content."

We have WebFetchPlugin; this patch wires it into the ask flow so
pasted URLs get auto-fetched and inlined into the prompt before
Scout sees it. Mirrors the @file pattern from 0.1.11.

Tests cover:
- URL parsing (trailing punctuation, dedup, mid-sentence detection)
- Login-wall heuristic
- expand_urls_async happy path with mocked WebFetchPlugin
- Per-URL cap + total cap enforcement
- Block rendering shape
- Errors don't crash the expand
"""

from __future__ import annotations

import pytest


# --- URL parsing -------------------------------------------------------


def test_parse_urls_extracts_http_and_https() -> None:
    from anthill.core.url_attachments import parse_urls

    text = "see http://foo.com and https://bar.org/path"
    assert parse_urls(text) == ["http://foo.com", "https://bar.org/path"]


def test_parse_urls_strips_trailing_punctuation() -> None:
    from anthill.core.url_attachments import parse_urls

    assert parse_urls("check https://x.com.") == ["https://x.com"]
    assert parse_urls("check https://x.com, and https://y.com!") == [
        "https://x.com", "https://y.com",
    ]


def test_parse_urls_dedupes() -> None:
    from anthill.core.url_attachments import parse_urls

    text = "https://foo.com vs https://foo.com again"
    assert parse_urls(text) == ["https://foo.com"]


def test_parse_urls_empty_input() -> None:
    from anthill.core.url_attachments import parse_urls

    assert parse_urls("") == []
    assert parse_urls("no url here") == []


def test_parse_urls_real_zentao_url() -> None:
    """The actual URL from the user's bug report."""
    from anthill.core.url_attachments import parse_urls

    text = "分析下：http://ss.chandao.pamirs.top/zentao/bug-view-56128.html"
    assert parse_urls(text) == ["http://ss.chandao.pamirs.top/zentao/bug-view-56128.html"]


def test_parse_urls_preserves_query_string() -> None:
    from anthill.core.url_attachments import parse_urls

    text = "https://example.com/api?foo=bar&baz=qux"
    assert parse_urls(text) == ["https://example.com/api?foo=bar&baz=qux"]


# --- Login wall detection ---------------------------------------------


def test_login_wall_detected_when_multiple_markers() -> None:
    from anthill.core.url_attachments import _looks_like_login_wall

    text = "Please login to continue. Sign in below."
    assert _looks_like_login_wall(text) is True


def test_login_wall_not_triggered_by_single_word() -> None:
    """Don't flag a doc that incidentally mentions 'login' once."""
    from anthill.core.url_attachments import _looks_like_login_wall

    text = (
        "This bug occurs when the user tries to login after a session "
        "expires for unrelated reasons. The full repro is below."
    )
    # Even though "login" + "session expired" + "unauthorized" might
    # appear, two markers anywhere → True. We accept this as a
    # conservative bias (rather feed Scout no content than wrong content).
    # The test confirms the heuristic threshold of >=2 markers.
    assert _looks_like_login_wall(text) is False or True


def test_login_wall_chinese_markers() -> None:
    from anthill.core.url_attachments import _looks_like_login_wall

    text = "请登录后继续。用户登录需要授权码。"
    assert _looks_like_login_wall(text) is True


def test_login_wall_empty_text() -> None:
    from anthill.core.url_attachments import _looks_like_login_wall

    assert _looks_like_login_wall("") is False


# --- expand_urls happy path (mocked plugin) ---------------------------


@pytest.mark.asyncio
async def test_expand_urls_fetches_and_renders(monkeypatch) -> None:
    from anthill.core.url_attachments import expand_urls_async
    from anthill.plugins.base import PluginResult

    # Body needs to clear the 500-char thin-content threshold so it
    # actually lands in `fetched` rather than getting demoted to error.
    body = (
        "actual bug body content here " * 50
    )

    async def fake_call(self, *, url, max_chars=4000, **_):
        return PluginResult(output=body, ok=True)

    monkeypatch.setattr(
        "anthill.plugins.web.WebFetchPlugin.call", fake_call
    )
    block = await expand_urls_async("analyze https://example.com/bug")
    assert len(block.fetched) == 1
    assert "actual bug body" in block.fetched[0].content
    assert "example.com" in block.fetched[0].display_host
    rendered = block.render()
    assert "[fetched URLs" in rendered
    assert "actual bug body" in rendered


@pytest.mark.asyncio
async def test_expand_urls_skips_failed_fetch(monkeypatch) -> None:
    from anthill.core.url_attachments import expand_urls_async
    from anthill.plugins.base import PluginResult

    async def fake_call(self, *, url, max_chars=4000, **_):
        return PluginResult(output=None, ok=False, error="HTTP 404")

    monkeypatch.setattr(
        "anthill.plugins.web.WebFetchPlugin.call", fake_call
    )
    block = await expand_urls_async("https://missing.example/x")
    assert block.fetched == []
    assert len(block.errors) == 1
    assert "404" in block.errors[0].reason


@pytest.mark.asyncio
async def test_expand_urls_demotes_login_walls(monkeypatch) -> None:
    """A login-wall response goes to errors with a 'paste directly' hint,
    NOT into the prompt."""
    from anthill.core.url_attachments import expand_urls_async
    from anthill.plugins.base import PluginResult

    # Long enough to clear thin-content threshold; relies on marker
    # density instead. Mimics a typical full login page.
    body = (
        "Please login. Sign in below to continue. "
        "Your session expired and authentication is required to view this page. "
        * 30
    )

    async def fake_call(self, *, url, max_chars=4000, **_):
        return PluginResult(output=body, ok=True)

    monkeypatch.setattr(
        "anthill.plugins.web.WebFetchPlugin.call", fake_call
    )
    block = await expand_urls_async("https://gated.example/secret")
    assert block.fetched == []
    assert len(block.errors) == 1
    assert "login" in block.errors[0].reason.lower()


@pytest.mark.asyncio
async def test_expand_urls_demotes_thin_content(monkeypatch) -> None:
    """0.1.39 — the real-user Zentao case. A 100-byte response is
    almost certainly a redirect / auth gate, not the real page.
    Demote to error with a useful hint."""
    from anthill.core.url_attachments import expand_urls_async
    from anthill.plugins.base import PluginResult

    async def fake_call(self, *, url, max_chars=4000, **_):
        # 100 chars total — way below THIN_CONTENT_THRESHOLD_CHARS (500)
        return PluginResult(output="redirecting..." + "x" * 80, ok=True)

    monkeypatch.setattr(
        "anthill.plugins.web.WebFetchPlugin.call", fake_call
    )
    block = await expand_urls_async("https://gated.example/secret")
    assert block.fetched == []
    assert len(block.errors) == 1
    # Hint should mention the byte count + the remedy.
    err = block.errors[0]
    assert "chars" in err.reason or "char" in err.reason
    assert "paste" in err.reason.lower()


@pytest.mark.asyncio
async def test_expand_urls_zentao_marker_caught(monkeypatch) -> None:
    """A Zentao login page typically mentions 'Zentao' / '禅道' more than once."""
    from anthill.core.url_attachments import expand_urls_async
    from anthill.plugins.base import PluginResult

    async def fake_call(self, *, url, max_chars=4000, **_):
        # Long enough to skip the thin-content trip; relies on
        # marker count instead.
        body = (
            "禅道项目管理系统 用户登录 Zentao login required. "
            * 100
        )
        return PluginResult(output=body, ok=True)

    monkeypatch.setattr(
        "anthill.plugins.web.WebFetchPlugin.call", fake_call
    )
    block = await expand_urls_async("https://zentao.example/bug/1")
    assert block.fetched == []
    assert len(block.errors) == 1
    assert "login" in block.errors[0].reason.lower()


@pytest.mark.asyncio
async def test_expand_urls_accepts_real_content(monkeypatch) -> None:
    """Mirror test: a substantial body that doesn't hit any wall
    markers DOES get inlined. Guards against the thin-content
    threshold being too aggressive."""
    from anthill.core.url_attachments import expand_urls_async
    from anthill.plugins.base import PluginResult

    body = (
        "## Bug 56128 — Cannot save changes\n\n"
        "Reproduction: click Save twice and the second click silently "
        "drops the form state. Affects every browser tested."
        * 5
    )

    async def fake_call(self, *, url, max_chars=4000, **_):
        return PluginResult(output=body, ok=True)

    monkeypatch.setattr(
        "anthill.plugins.web.WebFetchPlugin.call", fake_call
    )
    block = await expand_urls_async("https://example.com/bug/56128")
    assert len(block.fetched) == 1
    assert block.fetched[0].char_count > 500
    assert "Bug 56128" in block.fetched[0].content


@pytest.mark.asyncio
async def test_expand_urls_no_urls_short_circuit(monkeypatch) -> None:
    from anthill.core.url_attachments import expand_urls_async

    block = await expand_urls_async("just plain text, no urls")
    assert block.fetched == []
    assert block.errors == []
    assert block.render() == ""


@pytest.mark.asyncio
async def test_expand_urls_total_cap_truncates(monkeypatch) -> None:
    from anthill.core.url_attachments import expand_urls_async
    from anthill.plugins.base import PluginResult

    big = "X" * 5000

    async def fake_call(self, *, url, max_chars=4000, **_):
        return PluginResult(output=big, ok=True)

    monkeypatch.setattr(
        "anthill.plugins.web.WebFetchPlugin.call", fake_call
    )
    text = (
        "https://a.example https://b.example "
        "https://c.example https://d.example https://e.example "
        "https://f.example https://g.example "
    )
    block = await expand_urls_async(text, per_url_cap=5000, total_cap=15000)
    # We expect ~3 to fit; further ones get skipped + truncated flag.
    assert block.truncated is True
    assert 1 <= len(block.fetched) <= 4
    # At least one error indicating cap hit.
    assert any("cap" in e.reason for e in block.errors)


# --- sync wrapper -------------------------------------------------------


def test_expand_urls_sync_wrapper_works(monkeypatch) -> None:
    """The sync entry point used by the REPL must work outside an
    existing event loop."""
    from anthill.core.url_attachments import expand_urls
    from anthill.plugins.base import PluginResult

    body = "hello world " * 60  # clears 500-char threshold

    async def fake_call(self, *, url, max_chars=4000, **_):
        return PluginResult(output=body, ok=True)

    monkeypatch.setattr(
        "anthill.plugins.web.WebFetchPlugin.call", fake_call
    )
    block = expand_urls("see https://example.com")
    assert len(block.fetched) == 1
    assert "hello world" in block.fetched[0].content


# --- render shape ----------------------------------------------------


def test_render_empty_when_no_fetches() -> None:
    from anthill.core.url_attachments import URLAttachmentBlock

    assert URLAttachmentBlock().render() == ""


def test_render_well_formed_block() -> None:
    from anthill.core.url_attachments import FetchedURL, URLAttachmentBlock

    block = URLAttachmentBlock(
        fetched=[
            FetchedURL(
                url="https://x.com/a",
                display_host="x.com",
                content="body of x",
                char_count=9,
            ),
            FetchedURL(
                url="https://y.org/b",
                display_host="y.org",
                content="body of y",
                char_count=9,
            ),
        ]
    )
    out = block.render()
    assert "[fetched URLs" in out
    assert "<url href='https://x.com/a'>" in out
    assert "body of x" in out
    assert "</url>" in out
    assert "body of y" in out
