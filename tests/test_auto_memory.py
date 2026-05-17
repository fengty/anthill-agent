"""0.1.30 — auto-memory signal extraction.

0.1.29 gave us USER.md / MEMORY.md as plain text. 0.1.30 makes the
agent catch the most common "I want you to remember this" patterns
in user input — explicit only, conservative on purpose. The LLM-
driven "decide what's durable" pass is a separate later step.

Tests cover positive cases for each pattern family, negative cases
that shouldn't fire (markers in the middle, mention-without-claim),
and the cleaning / dedup logic.
"""

from __future__ import annotations


# --- direct memory imperative ---


def test_chinese_jizhu_user_preference() -> None:
    from anthill.core.auto_memory import TARGET_USER, extract_memory_signals

    sigs = extract_memory_signals("记住，我喜欢简洁回答")
    assert len(sigs) >= 1
    # Both "记住" and "我喜欢" patterns may fire; both target USER.md.
    assert all(s.target == TARGET_USER for s in sigs)
    assert any("简洁回答" in s.content for s in sigs)


def test_english_remember_that() -> None:
    from anthill.core.auto_memory import TARGET_USER, extract_memory_signals

    sigs = extract_memory_signals("remember that I'm based in Tokyo")
    assert len(sigs) == 1
    s = sigs[0]
    assert s.target == TARGET_USER
    assert s.section == "Preferences"
    assert "Tokyo" in s.content


def test_from_now_on() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals("From now on, give code answers in Python only")
    assert len(sigs) == 1
    assert "Python only" in sigs[0].content


def test_chinese_yihou() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals("以后都用中文回答我")
    assert len(sigs) == 1
    assert "中文回答" in sigs[0].content


# --- self-description ---


def test_chinese_wo_shi() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals("我是产品经理")
    assert len(sigs) == 1
    assert sigs[0].section == "Working style"
    assert "产品经理" in sigs[0].content


def test_english_i_am_a() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals("I am a backend engineer working on auth services")
    assert len(sigs) == 1
    assert sigs[0].section == "Working style"
    assert "backend engineer" in sigs[0].content.lower()


def test_i_work_on() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals("I work on multi-agent systems for finance")
    assert len(sigs) == 1
    assert sigs[0].section == "Working style"


# --- preferences ---


def test_i_prefer() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals("I prefer concise answers without preamble")
    assert len(sigs) == 1
    assert sigs[0].section == "Preferences"
    assert "concise" in sigs[0].content.lower()


def test_chinese_wo_xihuan() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals("我喜欢用 markdown 列表回答")
    assert len(sigs) == 1
    assert "markdown" in sigs[0].content.lower()


# --- nation-level facts ---


def test_project_uses_pattern() -> None:
    from anthill.core.auto_memory import TARGET_NATION, extract_memory_signals

    sigs = extract_memory_signals("This project uses uv for package management")
    assert len(sigs) == 1
    assert sigs[0].target == TARGET_NATION
    assert sigs[0].section == "Conventions"
    assert "uv" in sigs[0].content


def test_chinese_women_yong() -> None:
    from anthill.core.auto_memory import TARGET_NATION, extract_memory_signals

    sigs = extract_memory_signals("我们这里用 pnpm，不要用 npm")
    assert len(sigs) == 1
    assert sigs[0].target == TARGET_NATION
    assert "pnpm" in sigs[0].content


# --- negative cases ---


def test_marker_in_middle_does_not_fire() -> None:
    """The leader anchor protects against false positives mid-sentence."""
    from anthill.core.auto_memory import extract_memory_signals

    # "remember" appears, but not at sentence start.
    sigs = extract_memory_signals(
        "Earlier you said the project would remember its history"
    )
    assert sigs == []


def test_unrelated_question_does_not_fire() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    assert extract_memory_signals("What is the weather today?") == []
    assert extract_memory_signals("translate this paragraph to French") == []
    assert extract_memory_signals("你好") == []


def test_empty_input_returns_empty() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    assert extract_memory_signals("") == []
    assert extract_memory_signals("   ") == []


def test_extremely_short_after_marker_skipped() -> None:
    """Marker with a 1-char tail isn't worth saving — junk noise."""
    from anthill.core.auto_memory import extract_memory_signals

    # The cleaner requires len(gist) >= 2.
    sigs = extract_memory_signals("我喜欢 x")
    # Even if it matches, the content stays >= 2 char or we drop it.
    for s in sigs:
        assert len(s.content) >= 2


def test_dedup_same_target_same_gist() -> None:
    """If two patterns produce the same (target, gist), we keep one."""
    from anthill.core.auto_memory import extract_memory_signals

    # "记住，我喜欢简洁" — both 记住 and 我喜欢 could capture the same idea.
    sigs = extract_memory_signals("记住，我喜欢简洁回答")
    contents = [s.content for s in sigs]
    assert len(contents) == len(set(contents))


def test_content_is_length_capped() -> None:
    """Pathologically long capture gets trimmed at 200 chars."""
    from anthill.core.auto_memory import extract_memory_signals

    rant = "我喜欢 " + ("非常详细的回答 " * 80)
    sigs = extract_memory_signals(rant)
    assert len(sigs) == 1
    assert len(sigs[0].content) <= 201  # 200 + the ellipsis


def test_multiple_signals_in_one_input() -> None:
    """One ask can produce both a self-description and a preference."""
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals(
        "我是设计师。我喜欢用 figma 风格的回答"
    )
    sections = {s.section for s in sigs}
    assert "Working style" in sections
    assert "Preferences" in sections


def test_trailing_punctuation_stripped() -> None:
    from anthill.core.auto_memory import extract_memory_signals

    sigs = extract_memory_signals("I prefer concise answers.")
    assert sigs and not sigs[0].content.endswith(".")
