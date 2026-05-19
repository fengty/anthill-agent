"""0.1.73 — inline conversational auth prompt.

Replaces the /auth add command path with a "ask at the moment of
need" prompt. User feedback that drove this:

  > "增加 auth 命令是个很差的方案！别搞那么多命令，用户记不住的！"

The fix: when URL fetch hits a login wall AND no stored creds,
the REPL itself asks for username/password right then. The /auth
slash command handler stays for muscle memory but is no longer
advertised in HELP_TEXT.

These tests verify the inline-prompt helper functions in isolation
so we don't have to drive the full REPL.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from anthill.cli.repl import (
    _maybe_install_browser_interactively,
    _maybe_resolve_login_wall_interactively,
)
from anthill.core.url_attachments import FetchError, URLAttachmentBlock


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))
    return tmp_path


def _block_with_login_error(domain: str = "zentao.example.com") -> URLAttachmentBlock:
    block = URLAttachmentBlock()
    block.errors.append(
        FetchError(
            url=f"https://{domain}/page",
            reason=(
                "fetched but looks like a login wall. This page "
                "needs login — run /auth add to store credentials "
                "for the domain, then retry."
            ),
        )
    )
    return block


def _block_with_browser_missing_error() -> URLAttachmentBlock:
    block = URLAttachmentBlock()
    block.errors.append(
        FetchError(
            url="https://example.com/spa",
            reason=(
                "fetched only 100 chars. Browser fallback unavailable: "
                "Playwright not installed. Run /setup browser then retry."
            ),
        )
    )
    return block


# --- _maybe_resolve_login_wall_interactively ----------------------------


def test_login_prompt_skipped_when_not_a_tty(isolated_home) -> None:
    """In non-interactive contexts (daemon, piped) we must NOT prompt
    — input() would hang the process. Pass-through the block as-is."""
    block = _block_with_login_error()
    with patch("sys.stdin.isatty", return_value=False):
        out = _maybe_resolve_login_wall_interactively(
            "https://zentao.example.com/page", block
        )
    assert out is block  # unchanged


def test_login_prompt_skipped_when_no_login_error(isolated_home) -> None:
    """No login-wall error in the block → no prompt, even if TTY."""
    block = URLAttachmentBlock()
    block.errors.append(
        FetchError(url="https://x.example/y", reason="HTTP 500 server error")
    )
    with patch("sys.stdin.isatty", return_value=True):
        out = _maybe_resolve_login_wall_interactively(
            "https://x.example/y", block
        )
    assert out is block


def test_login_prompt_skipped_when_creds_already_stored(isolated_home) -> None:
    """If creds for the domain are already in secrets.toml, don't
    bother asking — the fallback chain would have used them. This
    error means login itself failed, not 'no creds'."""
    from anthill.core.url_credentials import (
        DomainCredentials,
        save_credentials,
    )

    save_credentials(DomainCredentials("zentao.example.com", "u", "p"))
    block = _block_with_login_error()
    with patch("sys.stdin.isatty", return_value=True):
        out = _maybe_resolve_login_wall_interactively(
            "https://zentao.example.com/page", block
        )
    assert out is block


def test_login_prompt_empty_username_aborts(isolated_home) -> None:
    """User pressing Enter at the username prompt = 'no thanks',
    pass-through original block."""
    block = _block_with_login_error()
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value=""):
        out = _maybe_resolve_login_wall_interactively(
            "https://zentao.example.com/page", block
        )
    assert out is block
    # And no creds were stored — verify.
    from anthill.core.url_credentials import load_credentials
    assert load_credentials("zentao.example.com") is None


def test_login_prompt_keyboard_interrupt_aborts(isolated_home) -> None:
    """Ctrl+C at the prompt = abort, pass-through original block."""
    block = _block_with_login_error()
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=KeyboardInterrupt):
        out = _maybe_resolve_login_wall_interactively(
            "https://zentao.example.com/page", block
        )
    assert out is block


def test_login_prompt_saves_creds_and_retries(isolated_home) -> None:
    """The happy path: user provides creds → they're stored → URL
    expand is re-run automatically with the new auth in place."""
    from anthill.core.url_credentials import load_credentials

    block = _block_with_login_error()

    # Simulate the interactive answers: username, password, login-url.
    # input() is called for username and login-url (2 calls); getpass
    # is called for password (1 call).
    inputs = iter(["alice", ""])  # alice for username, "" for login-url

    # The fresh expand_urls retry — return a "fetched" block this time.
    refreshed = URLAttachmentBlock()
    from anthill.core.url_attachments import FetchedURL
    refreshed.fetched.append(
        FetchedURL(
            url="https://zentao.example.com/page",
            display_host="zentao.example.com",
            content="real bug content here",
            char_count=21,
            via_browser=True,
        )
    )

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=lambda *a, **kw: next(inputs)), \
         patch("getpass.getpass", return_value="hunter2"), \
         patch(
             "anthill.core.url_attachments.expand_urls",
             return_value=refreshed,
         ):
        out = _maybe_resolve_login_wall_interactively(
            "https://zentao.example.com/page", block
        )

    # Creds were stored.
    creds = load_credentials("zentao.example.com")
    assert creds is not None
    assert creds.username == "alice"
    assert creds.password == "hunter2"
    # Retry happened — out has fetched content now.
    assert len(out.fetched) == 1
    assert out.fetched[0].content == "real bug content here"


# --- _maybe_install_browser_interactively -------------------------------


def test_browser_install_prompt_skipped_when_not_a_tty(isolated_home) -> None:
    block = _block_with_browser_missing_error()
    with patch("sys.stdin.isatty", return_value=False):
        out = _maybe_install_browser_interactively(
            "https://example.com/spa", block
        )
    assert out is block


def test_browser_install_prompt_skipped_when_no_browser_error(isolated_home) -> None:
    block = URLAttachmentBlock()
    block.errors.append(
        FetchError(url="https://x.example", reason="HTTP 500")
    )
    with patch("sys.stdin.isatty", return_value=True):
        out = _maybe_install_browser_interactively(
            "https://x.example", block
        )
    assert out is block


def test_browser_install_prompt_n_aborts(isolated_home) -> None:
    """User answering 'n' = no install, pass-through original block."""
    block = _block_with_browser_missing_error()
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="n"):
        out = _maybe_install_browser_interactively(
            "https://example.com/spa", block
        )
    assert out is block


def test_browser_install_prompt_y_triggers_install_and_retry(isolated_home) -> None:
    """User says 'y' (or Enter) → ensure_browser runs → URL re-fetched."""
    from anthill.core.browser_setup import (
        BrowserSetupResult,
        BrowserSetupState,
    )

    block = _block_with_browser_missing_error()

    # Stub ensure_browser to return success without actually
    # installing anything.
    fake_result = BrowserSetupResult(
        ok=True,
        state_before=BrowserSetupState(False, False),
        state_after=BrowserSetupState(True, True),
        steps_taken=["pip install playwright", "playwright install chromium"],
    )

    from anthill.core.url_attachments import URLAttachmentBlock as _Block
    fresh_block = _Block()  # post-install, fetch succeeded → empty errors

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="y"), \
         patch(
             "anthill.core.browser_setup.ensure_browser",
             return_value=fake_result,
         ), \
         patch(
             "anthill.core.url_attachments.expand_urls",
             return_value=fresh_block,
         ):
        out = _maybe_install_browser_interactively(
            "https://example.com/spa", block
        )

    # Got the refreshed block, not the original.
    assert out is fresh_block


def test_browser_install_prompt_empty_means_yes(isolated_home) -> None:
    """[Y/n] convention: empty input defaults to Yes."""
    from anthill.core.browser_setup import (
        BrowserSetupResult,
        BrowserSetupState,
    )

    block = _block_with_browser_missing_error()
    ensure_called: dict = {"count": 0}

    def fake_ensure(*, on_progress=None):
        ensure_called["count"] += 1
        return BrowserSetupResult(
            ok=True,
            state_before=BrowserSetupState(False, False),
            state_after=BrowserSetupState(True, True),
            steps_taken=[],
        )

    fresh_block = URLAttachmentBlock()
    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value=""), \
         patch(
             "anthill.core.browser_setup.ensure_browser",
             side_effect=fake_ensure,
         ), \
         patch(
             "anthill.core.url_attachments.expand_urls",
             return_value=fresh_block,
         ):
        _maybe_install_browser_interactively(
            "https://example.com/spa", block
        )
    assert ensure_called["count"] == 1
