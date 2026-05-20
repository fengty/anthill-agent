"""0.2.0 — short-circuit the ask when URL fetch failed AND the
request had nothing else to work with.

The live-session pattern this fixes:

  » 分析下：http://internal.zentao/bug-123
  ⚠ skipped ... (login wall)
  · [1] research running... I cannot fetch URLs...
    🛠 attempt 1 deferred — citizen punted the work back; retrying...
  · attempt 2: "Please paste the bug content..."
    🛠 attempt 2 deferred — ...
  · attempt 3: "I cannot access..."
  ✗ [1] research failed after 3 attempt(s)

3 LLM calls burned producing variants of "please paste content"
because there's literally no content to be resourceful with. The
0.1.40 refusal-retry path can't help here — citizens aren't being
unhelpful, the request is impossible.

This test pins the heuristic that triggers the short-circuit.
"""

from __future__ import annotations

from anthill.cli.repl import _request_is_essentially_just_url


def test_short_circuit_fires_on_pure_url() -> None:
    assert _request_is_essentially_just_url(
        "http://ss.chandao.pamirs.top/zentao/bug-view-56128.html"
    )


def test_short_circuit_fires_on_url_with_chinese_verb() -> None:
    """The exact live-session request."""
    assert _request_is_essentially_just_url(
        "分析下：http://ss.chandao.pamirs.top/zentao/bug-view-56128.html"
    )


def test_short_circuit_fires_on_url_with_english_verb() -> None:
    assert _request_is_essentially_just_url(
        "analyze https://jira.example.com/PROJ-123"
    )


def test_short_circuit_fires_on_check_url() -> None:
    assert _request_is_essentially_just_url(
        "check https://example.com/page"
    )


def test_short_circuit_skips_real_question_with_url() -> None:
    """URL is incidental — the question itself has substance."""
    assert not _request_is_essentially_just_url(
        "explain how rate limiting works and why https://stripe.com/"
        " is unusually permissive about it"
    )


def test_short_circuit_skips_long_substantive_request() -> None:
    """Long request that happens to contain a URL — don't short-circuit."""
    assert not _request_is_essentially_just_url(
        "I'm trying to understand the architecture of the system at "
        "https://example.com/docs and how it compares to alternatives "
        "like Postgres or MySQL."
    )


def test_short_circuit_skips_no_url() -> None:
    """No URL means the URL-fetch-failed short-circuit isn't relevant."""
    assert not _request_is_essentially_just_url(
        "write me a haiku about ants"
    )


def test_short_circuit_skips_empty_request() -> None:
    assert not _request_is_essentially_just_url("")
    assert not _request_is_essentially_just_url("   ")


def test_short_circuit_handles_punctuation_variants() -> None:
    """各种标点都该剥掉, 不影响判定."""
    for req in [
        "分析下：http://example.com",
        "分析下，http://example.com",
        "分析: http://example.com",
        "Look at this: https://example.com",
        "请帮我看下 https://example.com",
        "麻烦解读 https://example.com",
    ]:
        assert _request_is_essentially_just_url(req), req
