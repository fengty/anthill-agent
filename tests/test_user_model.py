"""0.1.32 — user-model inference from behavior signals.

The implicit-signal half of "越来越像你". 0.1.30 catches explicit
"remember X" markers; this patch derives preferences from how the
user actually uses the nation — language bias, length preference,
topic focus, time-of-day pattern.

Tests cover one positive + one negative scenario for each signal,
plus the dedup marker that prevents re-suggesting the same inference
on every session start.
"""

from __future__ import annotations

import time


def _entry(req: str = "x", task_types=("general",), *, ts=None):
    from anthill.core.history import HistoryEntry
    ts = ts if ts is not None else time.time()
    return HistoryEntry(
        id=HistoryEntry.make_id(req, ts),
        timestamp=ts,
        request=req,
        plan=[{"task_type": tt, "depends_on": []} for tt in task_types],
        outcomes=[{"status": "ok", "output": "fine"}],
    )


def _exemplar(rating: str, output: str = "ok"):
    from anthill.core.feedback import Exemplar
    return Exemplar(rating=rating, request="q", output=output, timestamp=0.0)


# --- language inference --------------------------------------------------


def test_language_chinese_first_when_cjk_dominates() -> None:
    from anthill.core.user_model import KIND_LANGUAGE, infer_user_model

    history = [_entry("帮我写一篇关于人工智能的总结") for _ in range(5)]
    out = infer_user_model(history, [])
    lang = [i for i in out if i.kind == KIND_LANGUAGE]
    assert len(lang) == 1
    assert "Chinese" in lang[0].summary


def test_language_english_first_when_latin_dominates() -> None:
    from anthill.core.user_model import KIND_LANGUAGE, infer_user_model

    history = [
        _entry("write me a thorough overview of multi agent systems in 2026")
        for _ in range(5)
    ]
    out = infer_user_model(history, [])
    lang = [i for i in out if i.kind == KIND_LANGUAGE]
    assert len(lang) == 1
    assert "English" in lang[0].summary


def test_language_no_inference_when_mixed() -> None:
    """Bilingual user shouldn't get either label."""
    from anthill.core.user_model import KIND_LANGUAGE, infer_user_model

    history = [
        _entry("帮我 review 这段 code 的 architecture"),
        _entry("translate this 段落 into English with explanation"),
    ]
    out = infer_user_model(history, [])
    assert not any(i.kind == KIND_LANGUAGE for i in out)


def test_language_no_inference_when_too_few_chars() -> None:
    """Below threshold, the signal is unreliable."""
    from anthill.core.user_model import KIND_LANGUAGE, infer_user_model

    history = [_entry("你好"), _entry("ok")]
    out = infer_user_model(history, [])
    assert not any(i.kind == KIND_LANGUAGE for i in out)


# --- length preference ---------------------------------------------------


def test_length_concise_when_up_shorter_than_down() -> None:
    from anthill.core.user_model import KIND_LENGTH, infer_user_model

    exemplars = [
        _exemplar("up", "short"),
        _exemplar("up", "still short"),
        _exemplar("up", "concise enough"),
        _exemplar("down", "very long answer " * 50),
        _exemplar("down", "another lengthy response " * 50),
        _exemplar("down", "yet more verbose " * 50),
    ]
    out = infer_user_model([], exemplars)
    length = [i for i in out if i.kind == KIND_LENGTH]
    assert len(length) == 1
    assert "concise" in length[0].summary.lower()


def test_length_detailed_when_up_longer_than_down() -> None:
    from anthill.core.user_model import KIND_LENGTH, infer_user_model

    exemplars = [
        _exemplar("up", "thorough explanation with examples " * 30),
        _exemplar("up", "deep dive with citations " * 30),
        _exemplar("up", "expansive coverage of topic " * 30),
        _exemplar("down", "short"),
        _exemplar("down", "ok"),
        _exemplar("down", "tldr"),
    ]
    out = infer_user_model([], exemplars)
    length = [i for i in out if i.kind == KIND_LENGTH]
    assert len(length) == 1
    assert "detailed" in length[0].summary.lower() or "thorough" in length[0].summary.lower()


def test_length_no_inference_without_enough_samples() -> None:
    from anthill.core.user_model import KIND_LENGTH, infer_user_model

    out = infer_user_model([], [_exemplar("up"), _exemplar("down")])
    assert not any(i.kind == KIND_LENGTH for i in out)


# --- topic focus ---------------------------------------------------------


def test_topics_surfaces_dominant_task_types() -> None:
    from anthill.core.user_model import KIND_TOPICS, infer_user_model

    history = (
        [_entry(task_types=("research",)) for _ in range(15)]
        + [_entry(task_types=("translate",)) for _ in range(10)]
        + [_entry(task_types=("draft",)) for _ in range(8)]
    )
    out = infer_user_model(history, [])
    topics = [i for i in out if i.kind == KIND_TOPICS]
    assert len(topics) == 1
    assert "research" in topics[0].summary
    assert "translate" in topics[0].summary


def test_topics_no_inference_for_thin_history() -> None:
    """Below the confidence threshold → nothing surfaced."""
    from anthill.core.user_model import KIND_TOPICS, infer_user_model

    history = [_entry(task_types=("general",))]
    out = infer_user_model(history, [])
    assert not any(i.kind == KIND_TOPICS for i in out)


# --- time of day ---------------------------------------------------------


def test_time_of_day_late_night_when_pattern_dominates() -> None:
    from anthill.core.user_model import KIND_TIME, infer_user_model

    # Build 10 entries all at 01:00 local — late-night bucket.
    base = time.mktime((2026, 5, 17, 1, 0, 0, 0, 0, -1))
    history = [_entry(ts=base + i * 3600 * 24) for i in range(10)]
    out = infer_user_model(history, [])
    tod = [i for i in out if i.kind == KIND_TIME]
    assert len(tod) == 1
    assert "late night" in tod[0].summary.lower()


def test_time_of_day_no_inference_when_spread() -> None:
    """Even distribution across hours → no dominant bucket."""
    from anthill.core.user_model import KIND_TIME, infer_user_model

    history = []
    for hour in range(24):
        ts = time.mktime((2026, 5, 17, hour, 0, 0, 0, 0, -1))
        history.append(_entry(ts=ts))
    out = infer_user_model(history, [])
    assert not any(i.kind == KIND_TIME for i in out)


# --- dedup marker --------------------------------------------------------


def test_already_recorded_matches_kind_marker() -> None:
    from anthill.core.user_model import KIND_LANGUAGE, Inference, already_recorded

    inf = Inference(
        kind=KIND_LANGUAGE,
        summary="prefers Chinese-first answers (auto-detected)",
        confidence=0.9,
    )
    user_md = (
        "## Languages / locales\n"
        "- 2026-05-17 prefers Chinese-first  <!-- auto:language -->\n"
    )
    assert already_recorded(inf, user_md) is True


def test_already_recorded_false_when_kind_missing() -> None:
    from anthill.core.user_model import KIND_TOPICS, Inference, already_recorded

    inf = Inference(kind=KIND_TOPICS, summary="x", confidence=0.9)
    user_md = "<!-- auto:language --> existing line"
    assert already_recorded(inf, user_md) is False


def test_only_high_confidence_inferences_returned() -> None:
    """The threshold filter: MIN_CONFIDENCE=0.7 is enforced."""
    from anthill.core.user_model import MIN_CONFIDENCE, infer_user_model

    # A two-entry history can't push any signal above 0.7.
    history = [_entry("anything"), _entry("anything else")]
    out = infer_user_model(history, [])
    for inf in out:
        assert inf.confidence >= MIN_CONFIDENCE
