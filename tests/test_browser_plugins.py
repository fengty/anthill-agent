"""Browser plugin tests.

Most tests run without Playwright installed (the optional extra) and
verify the safety/graceful-fail paths. Real browser tests are skipped
unless Playwright is importable AND chromium has been installed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anthill.plugins.browser import BrowserRenderPlugin, BrowserScreenshotPlugin


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def test_render_invalid_url() -> None:
    result = asyncio.run(BrowserRenderPlugin().call(url="not-a-url"))
    assert not result.ok
    assert "invalid url" in result.error


def test_screenshot_invalid_url() -> None:
    result = asyncio.run(BrowserScreenshotPlugin().call(url="javascript:alert(1)"))
    assert not result.ok
    assert "invalid url" in result.error


def test_render_helpful_error_without_playwright() -> None:
    """When playwright isn't installed, both plugins should explain how to fix it."""
    if _playwright_available():
        pytest.skip("playwright installed; install-hint path not testable here")
    result = asyncio.run(BrowserRenderPlugin().call(url="https://example.com"))
    assert not result.ok
    assert "[browser]" in result.error


def test_screenshot_helpful_error_without_playwright() -> None:
    if _playwright_available():
        pytest.skip("playwright installed; install-hint path not testable here")
    result = asyncio.run(BrowserScreenshotPlugin().call(url="https://example.com"))
    assert not result.ok
    assert "[browser]" in result.error


def test_screenshot_blocks_workspace_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even before Playwright kicks in, path-escape should be refused."""
    monkeypatch.setenv("ANTHILL_PLUGIN_WORKSPACE", str(tmp_path))
    # If playwright is missing, this hits the missing-lib path first;
    # if installed, it hits the escape check before opening a browser.
    result = asyncio.run(
        BrowserScreenshotPlugin().call(
            url="https://example.com", save_as="../../etc/screenshot.png"
        )
    )
    assert not result.ok
    # Either escape error or missing-lib hint — both prevent the unsafe write.
    assert ("escapes" in result.error) or ("[browser]" in result.error)
