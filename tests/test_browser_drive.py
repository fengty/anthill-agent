"""0.2.26 — browser driving for functional UI testing.

The marker parser + action dispatch are testable without a real
Playwright install: we stub `sess._page` with a recording mock and
assert the right method got called.

End-to-end Playwright tests (real chromium) live separately under
manual smoke tests; this file stays fast and doesn't touch the
network or open a browser.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from anthill.core.browser_drive import (
    BROWSER_TOOL_INSTRUCTION,
    BrowserBlock,
    BrowserSession,
    extract_browser_blocks,
    supported_actions,
)


# --- marker parsing ---------------------------------------------------


def test_extract_browser_markers_in_order() -> None:
    """Mixed actions in source order, args preserved including spaces."""
    text = (
        "let's: [[browser:goto https://example.com]] "
        "then [[browser:fill input#email user@example.com]] "
        "and [[browser:click button[type=submit]]] done"
    )
    blocks = extract_browser_blocks(text)
    assert [b.action for b in blocks] == ["goto", "fill", "click"]
    assert blocks[0].args == "https://example.com"
    assert blocks[1].args == "input#email user@example.com"
    assert blocks[2].args.startswith("button[type=submit]")


def test_extract_handles_whitespace_in_marker() -> None:
    text = "[[ browser : goto https://x.com ]]"
    blocks = extract_browser_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].action == "goto"
    assert blocks[0].args == "https://x.com"


def test_extract_action_lowercased() -> None:
    """The model might emit GOTO, Goto, etc. We normalize."""
    text = "[[browser:GOTO https://x.com]]"
    blocks = extract_browser_blocks(text)
    assert blocks[0].action == "goto"


def test_supported_actions_covers_qa_flow() -> None:
    """The actions a real functional test needs: navigate, click,
    fill, wait, text, screenshot. These names form the model's
    vocabulary — if we drop one the BROWSER_TOOL_INSTRUCTION lies."""
    actions = supported_actions()
    required = {"goto", "click", "fill", "wait", "text", "screenshot"}
    assert required.issubset(set(actions)), (
        f"missing required actions: {required - set(actions)}"
    )


# --- action dispatch on a mocked page --------------------------------


def _mock_session() -> tuple[BrowserSession, mock.AsyncMock]:
    """Build a session with `_page` replaced by an AsyncMock so we
    can verify which Playwright methods get called."""
    sess = BrowserSession(state_dir=None)
    page = mock.AsyncMock()
    page.url = "https://current.example.com"
    sess._page = page
    return sess, page


def test_goto_calls_page_goto() -> None:
    sess, page = _mock_session()
    result = asyncio.run(sess.execute("goto", "https://example.com"))
    assert result.ok
    page.goto.assert_awaited_once_with("https://example.com")


def test_click_calls_page_click() -> None:
    sess, page = _mock_session()
    asyncio.run(sess.execute("click", "button.submit"))
    page.click.assert_awaited_once_with("button.submit")


def test_fill_splits_selector_and_value() -> None:
    """`fill SELECTOR VALUE` — first whitespace separates the two."""
    sess, page = _mock_session()
    asyncio.run(sess.execute("fill", "input#email user@example.com"))
    page.fill.assert_awaited_once_with("input#email", "user@example.com")


def test_fill_value_can_have_spaces() -> None:
    """The VALUE portion of `fill SELECTOR VALUE` should preserve
    spaces — addresses / sentences / form text often have them."""
    sess, page = _mock_session()
    asyncio.run(sess.execute("fill", "textarea#bio Hello world from anthill"))
    page.fill.assert_awaited_once_with(
        "textarea#bio", "Hello world from anthill"
    )


def test_text_returns_element_content() -> None:
    sess, page = _mock_session()
    page.text_content.return_value = "Welcome, Alice!"
    result = asyncio.run(sess.execute("text", "h1.welcome"))
    assert result.ok
    assert result.value == "Welcome, Alice!"


def test_wait_default_visible() -> None:
    sess, page = _mock_session()
    asyncio.run(sess.execute("wait", ".dashboard"))
    page.wait_for_selector.assert_awaited_once_with(
        ".dashboard", state="visible"
    )


def test_wait_explicit_state() -> None:
    sess, page = _mock_session()
    asyncio.run(sess.execute("wait", ".loader hidden"))
    page.wait_for_selector.assert_awaited_once_with(
        ".loader", state="hidden"
    )


def test_url_returns_current_url() -> None:
    sess, page = _mock_session()
    result = asyncio.run(sess.execute("url", ""))
    assert result.value == "https://current.example.com"


def test_unknown_action_returns_error() -> None:
    """Don't crash on unknown — return a structured error so the
    model sees it and can correct."""
    sess, page = _mock_session()
    result = asyncio.run(sess.execute("teleport", "to mars"))
    assert not result.ok
    assert "unknown action" in result.error


def test_action_exception_caught_as_error() -> None:
    """Playwright timeouts / selector-not-found shouldn't crash the
    REPL — they come back as result.ok=False with the error msg."""
    sess, page = _mock_session()
    page.click.side_effect = RuntimeError("Timeout: selector not found")
    result = asyncio.run(sess.execute("click", "#missing"))
    assert not result.ok
    assert "Timeout" in result.error


def test_fill_requires_two_args() -> None:
    """`fill SELECTOR` without a VALUE → error, not crash."""
    sess, page = _mock_session()
    result = asyncio.run(sess.execute("fill", "input.lone"))
    assert not result.ok
    assert "fill requires" in result.error
