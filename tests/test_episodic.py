"""Tests for episodic memory (TF-IDF semantic search over history)."""

from __future__ import annotations

from anthill.core.episodic import (
    TfidfIndex,
    find_similar,
    format_similar_for_scout,
    tokenize,
)
from anthill.core.history import HistoryEntry


def _entry(req: str, ts: float = 1.0) -> HistoryEntry:
    return HistoryEntry(
        id=HistoryEntry.make_id(req, ts),
        timestamp=ts,
        request=req,
        plan=[{"task_type": "x", "depends_on": []}],
        outcomes=[],
    )


def test_tokenize_english() -> None:
    assert tokenize("Translate Hello, World!") == ["translate", "hello", "world"]


def test_tokenize_cjk_character_level() -> None:
    # CJK Unified Ideographs (U+4E00..U+9FFF) — characters used across
    # Chinese, Japanese kanji, and Korean hanja. The tokenizer treats
    # each one as its own token because CJK scripts lack reliable word
    # boundaries.
    toks = tokenize("文字解析機構")
    assert "文" in toks
    assert "析" in toks
    assert len(toks) == 6


def test_tokenize_mixed_script() -> None:
    toks = tokenize("text 翻 訳 entry")
    assert "text" in toks
    assert "entry" in toks
    assert "翻" in toks
    assert "訳" in toks




def test_index_finds_similar_doc() -> None:
    index = TfidfIndex([
        "translate hello to japanese",
        "summarise this PDF",
        "translate goodbye to japanese",
    ])
    hits = index.query("translate good morning to japanese", top_k=2)
    assert len(hits) >= 1
    # Best match should be one of the translate ones.
    idx_best, _ = hits[0]
    assert idx_best in (0, 2)


def test_index_returns_empty_on_empty_corpus() -> None:
    assert TfidfIndex([]).query("anything") == []


def test_index_returns_empty_on_empty_query() -> None:
    index = TfidfIndex(["something"])
    assert index.query("") == []


def test_find_similar_filters_by_min_score() -> None:
    history = [_entry("translate hello to japanese")]
    hits = find_similar("unrelated meta-philosophy", history, min_score=0.5)
    assert hits == []


def test_find_similar_returns_top_k() -> None:
    history = [
        _entry("translate alpha to japanese", 1.0),
        _entry("translate beta to japanese", 2.0),
        _entry("translate gamma to japanese", 3.0),
        _entry("write a poem about cats", 4.0),
    ]
    hits = find_similar("translate delta to japanese", history, top_k=2, min_score=0.0)
    assert len(hits) == 2
    assert all("translate" in h.entry.request for h in hits)


def test_format_for_scout_omits_when_empty() -> None:
    assert format_similar_for_scout([]) == ""


def test_format_for_scout_includes_plan_types() -> None:
    history = [_entry("translate hello to japanese")]
    history[0].plan = [
        {"task_type": "translate", "depends_on": []},
        {"task_type": "verify", "depends_on": ["translate"]},
    ]
    hits = find_similar("translate hi to japanese", history, top_k=1, min_score=0.0)
    formatted = format_similar_for_scout(hits)
    assert "translate -> verify" in formatted
