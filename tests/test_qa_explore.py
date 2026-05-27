"""0.2.45 — sparse-requirement + URL triggers exploration step.

Real bug (from production session):
  » /test 我需要进行测试 http://localhost:3000/
    ✗ couldn't parse test cases from model output.
    raw: []
    []
    []

The LLM provider can't reach the user's localhost, so the case
generator had no idea what's on the page → returned `[]`. Fix:
when the requirement is sparse AND contains a URL, FIRST have
a citizen open the URL via browser_action and report what's
there, then prepend that report to the requirement for the
case-gen step.

These tests cover the `is_sparse_requirement_with_url` heuristic
(end-to-end exploration via REPL is hand-tested; mocking the full
REPL+browser path is too brittle for unit tests).
"""

from __future__ import annotations

import pytest

from anthill.core.qa import (
    EXPLORE_FOR_QA_PROMPT,
    is_sparse_requirement_with_url,
)


# --- positive: explore-worthy inputs ---------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Bare URL is the canonical sparse case.
        "http://localhost:3000/",
        "test http://localhost:3000/",
        "测试 http://localhost:3000",
        # The exact production failure.
        "我需要进行测试 http://localhost:3000/",
        # Sparse English variants.
        "please test https://app.example.com/",
        # URL with trailing fluff that's still vague.
        "http://x.com/page 帮我看看",
    ],
)
def test_sparse_requirement_with_url_returns_url(text: str) -> None:
    """All these should trigger exploration: the requirement
    doesn't say what to test, just where."""
    result = is_sparse_requirement_with_url(text)
    assert result is not None
    assert result.startswith("http")


# --- negative: rich requirements don't need exploration -------------


@pytest.mark.parametrize(
    "text",
    [
        # No URL → nothing to explore.
        "test the login flow with wrong password",
        "",
        # URL present but the requirement is detailed enough.
        "test http://x.com/login: type wrong password, expect "
        "'invalid credentials' visible in red below the form",
        "在 http://localhost:3000 用 admin/admin 登录后看商品列表至少 "
        "显示 10 条, 第一条点击进入详情页验证标题非空",
    ],
)
def test_rich_or_no_url_requirements_skip_exploration(text: str) -> None:
    """Rich requirements have enough specificity already; no URL
    means we can't explore even if we wanted to."""
    assert is_sparse_requirement_with_url(text) is None


# --- exact extraction --------------------------------------------


def test_url_extracted_correctly_stripping_trailing_punctuation() -> None:
    """The URL match shouldn't include trailing punctuation
    (., 。, !, ?) that's almost certainly not part of the URL."""
    result = is_sparse_requirement_with_url("test http://x.com/page.")
    assert result == "http://x.com/page"


def test_url_preserves_path_query_fragment() -> None:
    """Real URLs have ?query and #fragment — keep them."""
    url = "http://localhost:3000/admin?tab=products&page=2"
    result = is_sparse_requirement_with_url(f"测试 {url}")
    assert result == url


# --- prompt sanity ----------------------------------------------


def test_explore_prompt_substitutes_url_and_calls_browser() -> None:
    """The exploration prompt must (a) tell the citizen what URL,
    and (b) explicitly direct them to use the browser tool — without
    that, the citizen would just describe a generic page."""
    rendered = EXPLORE_FOR_QA_PROMPT.replace("{url}", "http://x.com/foo")
    assert "http://x.com/foo" in rendered
    # Must instruct browser usage explicitly.
    assert "[[browser:" in rendered or "browser tool" in rendered.lower()
    # Must ask for structured output the case-gen step can use.
    assert "PAGE TITLE" in rendered or "VISIBLE" in rendered
