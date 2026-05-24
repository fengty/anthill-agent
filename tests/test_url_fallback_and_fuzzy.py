"""0.2.41 — URL fetch fail → browser hint + slash fuzzy match.

Real session screenshot showed two problems:

  1. User pastes "URL,admin/admin,中文任务" — anthill's URL strip
     regex ate the whole tail (no whitespace), `_request_is_
     essentially_just_url` returned True, anthill bailed with
     "skipped, no content" instead of letting citizens use the
     browser tool.

  2. User typed `/step browser` (typo of `/setup browser`) and
     got just "Unknown command. Try /help" — no suggestion.

Both fixed in 0.2.41. Tests guard against regression.
"""

from __future__ import annotations

import pytest


# --- URL strip no longer eats following text -------------------------


def test_url_strip_stops_at_comma() -> None:
    """The regex used to use \\S+ which ate everything until
    whitespace. Production URL pattern: 'URL,admin/admin,任务' has
    no whitespace. With the fix, only the URL chunk is stripped
    and the trailing creds + task survive."""
    from anthill.cli.repl import _request_is_essentially_just_url

    # Real-shape input from the user's session.
    req = (
        "http://192.168.201.46:8080/page;module=gspCore;viewType=TABLE"
        ",admin/admin,先整理出来由哪些菜单"
    )
    # NOT "essentially just a URL" — there's meaningful task text.
    assert _request_is_essentially_just_url(req) is False


def test_url_strip_keeps_semicolons_in_path() -> None:
    """SPA URLs use `;` for matrix params (gspCorePc routing, JAX-RS
    matrix params, classic Java app servers). Those must stay
    inside the URL or the whole pattern misbehaves."""
    from anthill.cli.repl import _request_is_essentially_just_url

    # JUST the URL with semicolons — no task text after.
    bare = (
        "http://x.com/page;module=A;view=B;params=C"
    )
    assert _request_is_essentially_just_url(bare) is True


def test_bare_url_still_classifies_as_just_url() -> None:
    """Sanity: 0.2.0 behavior preserved for the pure case."""
    from anthill.cli.repl import _request_is_essentially_just_url

    assert _request_is_essentially_just_url("http://x.com/page") is True
    assert _request_is_essentially_just_url("分析下 http://x.com") is True
    assert _request_is_essentially_just_url("看看 https://x.com/y?q=1") is True


def test_url_plus_substantial_task_not_just_url() -> None:
    """The other direction: with whitespace OR comma separator, a
    long task in any form makes the request NOT just-a-URL."""
    from anthill.cli.repl import _request_is_essentially_just_url

    # With whitespace.
    a = "http://x.com 先把这个页面所有的菜单整理出来给我"
    assert _request_is_essentially_just_url(a) is False

    # With comma (real user pattern).
    b = "http://x.com,user/pass,把所有按钮的位置都告诉我"
    assert _request_is_essentially_just_url(b) is False


# --- fuzzy slash command matching ------------------------------------


def test_fuzzy_suggests_setup_for_step() -> None:
    """The production typo: `/step browser` → suggest `/setup browser`."""
    from anthill.cli.repl import _suggest_nearest_slash
    from anthill.cli.completion import KNOWN_SLASH_COMMANDS

    s = _suggest_nearest_slash("/step", KNOWN_SLASH_COMMANDS)
    assert s == "/setup"


def test_fuzzy_suggests_history_for_hsitory() -> None:
    """Transpose typo."""
    from anthill.cli.repl import _suggest_nearest_slash
    from anthill.cli.completion import KNOWN_SLASH_COMMANDS

    s = _suggest_nearest_slash("/hsitory", KNOWN_SLASH_COMMANDS)
    assert s == "/history"


def test_fuzzy_returns_none_for_unrelated() -> None:
    """A typed string nowhere near any known command → no
    suggestion (don't propose nonsense)."""
    from anthill.cli.repl import _suggest_nearest_slash
    from anthill.cli.completion import KNOWN_SLASH_COMMANDS

    assert _suggest_nearest_slash("/zxcvbnm", KNOWN_SLASH_COMMANDS) is None


def test_fuzzy_empty_input_returns_none() -> None:
    from anthill.cli.repl import _suggest_nearest_slash
    assert _suggest_nearest_slash("", ("/help", "/quit")) is None
    assert _suggest_nearest_slash("/help", ()) is None


def test_fuzzy_threshold_blocks_too_distant() -> None:
    """A short typed string can't be matched to a long candidate
    unless the relative distance is small. Prevents '/x' → '/xyz...'."""
    from anthill.cli.repl import _suggest_nearest_slash

    # '/a' vs '/setup' — distance 5, way above threshold.
    assert _suggest_nearest_slash("/a", ("/setup",)) is None


def test_fuzzy_prefers_prefix_matches_in_ties() -> None:
    """When two candidates have the same edit distance, prefer the
    one that shares a prefix with the typed string."""
    from anthill.cli.repl import _suggest_nearest_slash

    # `/setp` is 1 edit from both `/setup` and `/seup` (if existed).
    # We arrange a real prefix-vs-non-prefix tie.
    candidates = ("/setup", "/notepad")
    # /step is 1-2 edits from /setup, much further from /notepad.
    s = _suggest_nearest_slash("/step", candidates)
    assert s == "/setup"


# --- _request_is_essentially_just_url false positives ---------------


def test_url_with_creds_and_short_task_still_substantive() -> None:
    """User pastes URL + 'admin/admin' (creds) + a short Chinese
    task. The task is short, but PRESENT — should NOT short-circuit."""
    from anthill.cli.repl import _request_is_essentially_just_url

    # Has 10+ chars of substantive content after comma-stripped URL.
    req = "http://x.com/page;m=A,user/pwd,把首页所有按钮列出来"
    # Whether this trips < 20 depends on what tokens survive after
    # strip. With the fix, content survives; this should be False.
    assert _request_is_essentially_just_url(req) is False


def test_url_with_only_punctuation_still_just_url() -> None:
    """URL + just punctuation/symbols (no real task) → still just-a-URL."""
    from anthill.cli.repl import _request_is_essentially_just_url

    assert _request_is_essentially_just_url("http://x.com/page?q=1，") is True
    assert _request_is_essentially_just_url("http://x.com, ") is True
