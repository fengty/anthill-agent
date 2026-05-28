"""0.2.46 — collapse Chinese-IME-injected spaces.

Real production failure (user screenshot):
    » /test r e c o r d我试 http://localhost:3000/
    bye.

macOS Chinese IMEs in some states insert spaces between every
Latin character a user types. anthill saw the command as gibberish,
user got frustrated, hit Ctrl+D, exited.

Heuristic: 3+ consecutive single-letter Latin tokens separated by
single spaces is IME spacing — never intentional. Collapse them.
Real commands keep their spaces (`ls -la foo`).
"""

from __future__ import annotations

import pytest

from anthill.cli.repl import _normalize_imed_input


# --- positive: IME spacing collapses --------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Exact production case. The IME glued `d` to `我试`;
        # we peel the lone Latin prefix back onto the collapsed
        # run and split the CJK tail into its own token (better
        # than leaving `record我试` glued).
        (
            "/test r e c o r d我试 http://localhost:3000/",
            "/test record 我试 http://localhost:3000/",
        ),
        # Bare IME-spaced word.
        ("r e c o r d", "record"),
        # IME spacing mid-sentence.
        ("git s t a t u s", "git status"),
        # All-uppercase too.
        ("p i n g 192.168.1.149", "ping 192.168.1.149"),
        # Run followed by a non-Latin word (no space): the
        # trailing 'd' isn't part of "d我试" until split-on-space.
        # Our regex correctly keeps the 'd' in the run because
        # there's no whitespace between d and 我.
        (
            "/retest l a t e s t",
            "/retest latest",
        ),
    ],
)
def test_ime_collapse(raw: str, expected: str) -> None:
    normalized, fixed = _normalize_imed_input(raw)
    assert normalized == expected
    assert fixed is True


# --- negative: real commands stay intact ---------------------------


@pytest.mark.parametrize(
    "raw",
    [
        # Normal multi-token command — words are ≥2 chars, no
        # collapse trigger.
        "ls -la foo bar",
        "git status",
        "/test record http://x.com",
        # Single letter flags (-l, -a) shouldn't be collapsed
        # because they have hyphens separating them from each other.
        "ls -l -a",
        # Empty / whitespace-only.
        "",
        "   ",
        # Mostly CJK — IME wouldn't be inserting spaces here anyway.
        "测试一下登录功能",
        # Two single ASCII letters separated by space — below
        # threshold of 3+, leave alone.
        "x y",
        # Three short words that are NOT single letters.
        "do re mi",
    ],
)
def test_real_input_unchanged(raw: str) -> None:
    normalized, fixed = _normalize_imed_input(raw)
    assert normalized == raw
    assert fixed is False


# --- threshold tuning ---------------------------------------------


def test_threshold_is_three_or_more_consecutive() -> None:
    """2 letters in a row aren't enough signal (could be 'x y');
    3 trip the heuristic ('a b c' → 'abc'). Adjust if real users
    type a lot of 3-letter combos with intentional spaces."""
    # 2 letters: leave alone.
    out2, fixed2 = _normalize_imed_input("a b foo")
    assert out2 == "a b foo" and not fixed2
    # 3 letters: collapse.
    out3, fixed3 = _normalize_imed_input("a b c foo")
    assert out3 == "abc foo" and fixed3


def test_returns_unchanged_pair_when_no_change() -> None:
    """Caller relies on `was_normalized` to decide whether to
    print the "🔤 IME fixed" notice. Don't lie about fixes."""
    line = "hello world"
    out, fixed = _normalize_imed_input(line)
    assert out == line
    assert fixed is False
