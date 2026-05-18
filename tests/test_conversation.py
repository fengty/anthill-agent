"""0.1.28 — conversation memory + follow-up detection.

The exact bug a real user reported in one screenshot:

  » 最近热门电影            (gets a wrong-era answer)
  » 我说的是 2026 年的       (citizen forgot the previous turn,
                              asked "什么 2026 年的话题?")

The fast_classify pre-Scout heuristic treated "我说的是 2026 年的"
as trivial (≤5 words, no complex markers), bypassed Scout entirely,
no prior-turn context was injected, and the citizen answered as if
seeing a fresh prompt.

This patch:
- Adds follow-up markers to complexity.fast_classify so short
  continuations stop being misclassified as trivial
- New core/conversation.py with a rolling ConversationContext
- REPL keeps one ConversationContext per session, injects via
  wrap_with_context when is_follow_up fires, and resets on
  /clear or /nation
"""

from __future__ import annotations


# --- fast_classify follow-up override -------------------------------------


def test_follow_up_marker_chinese_blocks_trivial() -> None:
    """The exact phrase from the bug report."""
    from anthill.core.complexity import fast_classify

    assert fast_classify("我说的是 2026 年的") is None  # NOT trivial


def test_follow_up_marker_english_blocks_trivial() -> None:
    from anthill.core.complexity import fast_classify

    assert fast_classify("tell me more") is None
    assert fast_classify("what about Chinese tools") is None
    assert fast_classify("I meant 2026") is None


def test_short_trivial_still_works_without_markers() -> None:
    """Backward compat: bare 'hi' is still trivial."""
    from anthill.core.complexity import fast_classify

    assert fast_classify("hi") == "trivial"
    assert fast_classify("你好") == "trivial"


def test_complex_marker_still_wins_over_followup() -> None:
    """If the input has BOTH a follow-up marker AND a complex
    marker (e.g. '再深入研究 X'), complexity wins."""
    from anthill.core.complexity import fast_classify

    assert fast_classify("再深入研究一下 AI 主流网站") == "complex"


# --- ConversationContext -------------------------------------------------


def test_context_records_and_bounds() -> None:
    from anthill.core.conversation import ConversationContext

    c = ConversationContext(maxlen=3)
    for i in range(5):
        c.record(f"q{i}", f"a{i}")
    # Maxlen=3 ⇒ only last 3 retained.
    turns = c.recent()
    assert [t.request for t in turns] == ["q2", "q3", "q4"]


def test_context_ignores_empty_request() -> None:
    from anthill.core.conversation import ConversationContext

    c = ConversationContext()
    c.record("", "a")
    assert len(c) == 0
    c.record("real", "ok")
    assert len(c) == 1


def test_context_reset_clears() -> None:
    from anthill.core.conversation import ConversationContext

    c = ConversationContext()
    c.record("q", "a")
    c.reset()
    assert c.recent() == []
    assert c.last_turn() is None


def test_context_last_turn_returns_most_recent() -> None:
    from anthill.core.conversation import ConversationContext

    c = ConversationContext()
    c.record("first", "1")
    c.record("second", "2")
    last = c.last_turn()
    assert last is not None
    assert last.request == "second"


# --- is_follow_up ---------------------------------------------------------


def test_is_follow_up_returns_false_when_no_prior() -> None:
    from anthill.core.conversation import is_follow_up

    assert is_follow_up("我说的是 2026 年的", None) is False


def test_is_follow_up_true_for_marker_with_prior() -> None:
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("最近热门电影", "(answer)")
    assert is_follow_up("我说的是 2026 年的", c.last_turn()) is True


def test_is_follow_up_true_for_short_input_with_prior() -> None:
    """Generic short input + prior turn → wrap. Cheap to over-wrap."""
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("最近热门电影", "(answer)")
    assert is_follow_up("具体点", c.last_turn()) is True


def test_is_follow_up_false_for_long_standalone_with_prior() -> None:
    """A long fresh question shouldn't get auto-wrapped just because
    a prior turn happens to exist — that would over-context every ask."""
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("最近热门电影", "(answer)")
    fresh = (
        "research the top 3 open-source vector databases and "
        "recommend one for our use case with latency requirements"
    )
    assert is_follow_up(fresh, c.last_turn()) is False


def test_is_follow_up_empty_input_is_false() -> None:
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("q", "a")
    assert is_follow_up("   ", c.last_turn()) is False


# --- 0.1.48 — trivial pleasantries are conversation resets, not follow-ups


def test_is_follow_up_chinese_greeting_after_bug_analysis_is_reset() -> None:
    """The original bug: 你好 after a bug-analysis turn was being
    wrapped as a follow-up because it's short, then Scout saw the
    bug-analysis context and planned research+analyze for a greeting
    — 45s wasted on a "hi". A greeting MUST be its own conversation
    starting point."""
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("分析下：http://example.com/zentao/bug-56128.html", "(long bug analysis)")
    assert is_follow_up("你好", c.last_turn()) is False
    assert is_follow_up("您好", c.last_turn()) is False


def test_is_follow_up_english_greeting_after_real_ask_is_reset() -> None:
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("write a report on Q3 revenue", "(long report)")
    assert is_follow_up("hi", c.last_turn()) is False
    assert is_follow_up("hello", c.last_turn()) is False
    assert is_follow_up("thanks", c.last_turn()) is False


def test_is_follow_up_pleasantry_with_punctuation_still_reset() -> None:
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("分析bug", "(answer)")
    assert is_follow_up("你好！", c.last_turn()) is False
    assert is_follow_up("Hi.", c.last_turn()) is False


def test_is_follow_up_greeting_with_real_content_is_NOT_reset() -> None:
    """If user says "你好 can you also analyze this", we still want
    the prior context. The trivial-reset only fires when the message
    IS the pleasantry, not when it starts with one."""
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("分析bug 1234", "(answer)")
    # Short + non-trivial → follow-up wrap fires (correct behavior).
    assert is_follow_up("你好 还有什么发现", c.last_turn()) is True


# --- wrap_with_context ---------------------------------------------------


def test_wrap_with_context_returns_input_when_no_history() -> None:
    from anthill.core.conversation import wrap_with_context

    assert wrap_with_context("X", []) == "X"


def test_wrap_with_context_inlines_prior_turn() -> None:
    from anthill.core.conversation import ConversationContext, wrap_with_context

    c = ConversationContext()
    c.record("最近热门电影", "(some movies from 2024)")
    wrapped = wrap_with_context("我说的是 2026 年的", c.recent())
    # Both pieces present.
    assert "最近热门电影" in wrapped
    assert "(some movies from 2024)" in wrapped
    assert "我说的是 2026 年的" in wrapped
    # Header phrasing so Scout knows to read it as context.
    assert "[recent conversation" in wrapped
    assert "follow-up" in wrapped


def test_wrap_with_context_truncates_long_turns() -> None:
    """A 100K-char prior answer must not blow the prompt window."""
    from anthill.core.conversation import ConversationContext, wrap_with_context

    huge_answer = "A" * 50_000
    c = ConversationContext()
    c.record("question", huge_answer)
    wrapped = wrap_with_context("follow up", c.recent(), max_chars_per_turn=1000)
    # Truncated marker present, total bounded.
    assert "…" in wrapped
    assert len(wrapped) < 5_000


def test_wrap_with_context_preserves_turn_order() -> None:
    """TURN 1 / TURN 2 / TURN 3 should appear in chronological order."""
    from anthill.core.conversation import ConversationContext, wrap_with_context

    c = ConversationContext()
    c.record("first", "ans1")
    c.record("second", "ans2")
    c.record("third", "ans3")
    wrapped = wrap_with_context("now", c.recent())
    assert wrapped.index("first") < wrapped.index("second") < wrapped.index("third")
