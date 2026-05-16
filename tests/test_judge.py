"""Tests for the LLM judge (parsing only — no real API calls)."""

from __future__ import annotations

import os

import pytest

from anthill.core.judge import judge_enabled, parse_verdict


def test_parse_clean_json() -> None:
    v = parse_verdict('{"score": 0.85, "reason": "good answer"}')
    assert abs(v.score - 0.85) < 1e-6
    assert "good answer" in v.reason


def test_parse_clamps_to_unit() -> None:
    high = parse_verdict('{"score": 1.5, "reason": "x"}')
    assert high.score == 1.0
    low = parse_verdict('{"score": -0.4, "reason": "x"}')
    assert low.score == 0.0


def test_parse_handles_code_fence() -> None:
    text = '```json\n{"score": 0.5, "reason": "ok"}\n```'
    v = parse_verdict(text)
    assert v.score == 0.5


def test_parse_falls_back_on_nonsense() -> None:
    v = parse_verdict("the judge has fallen ill")
    assert v.score == 0.5
    assert "unparseable" in v.reason or "no JSON" in v.reason


def test_parse_extracts_embedded_object() -> None:
    text = 'Some prose before {"score": 0.7, "reason": "fine"} and after'
    v = parse_verdict(text)
    assert abs(v.score - 0.7) < 1e-6


def test_parse_handles_string_score() -> None:
    """Some models return score as a string. Coerce."""
    v = parse_verdict('{"score": "0.6", "reason": "ok"}')
    assert abs(v.score - 0.6) < 1e-6


def test_parse_neutral_on_non_numeric_score() -> None:
    v = parse_verdict('{"score": "great", "reason": "x"}')
    assert v.score == 0.5


def test_judge_enabled_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_USE_JUDGE", "1")
    assert judge_enabled() is True
    monkeypatch.setenv("ANTHILL_USE_JUDGE", "")
    assert judge_enabled() is False
    monkeypatch.setenv("ANTHILL_USE_JUDGE", "true")
    assert judge_enabled() is True
