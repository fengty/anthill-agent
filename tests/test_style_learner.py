"""Tests for the style learner (formatting + thin-data refusal)."""

from __future__ import annotations

import asyncio

from anthill.core.feedback import Exemplar
from anthill.core.style_learner import format_exemplars_for_prompt, suggest_house_style


def test_format_separates_approved_and_rejected() -> None:
    exemplars = [
        Exemplar("up", "tell me about X", "X is great", 1.0),
        Exemplar("down", "tell me about Y", "Y is verbose", 2.0),
    ]
    formatted = format_exemplars_for_prompt(exemplars)
    assert "APPROVED" in formatted
    assert "REJECTED" in formatted
    assert "X is great" in formatted
    assert "Y is verbose" in formatted


def test_format_handles_only_approved() -> None:
    exemplars = [Exemplar("up", "r", "o", 1.0)]
    formatted = format_exemplars_for_prompt(exemplars)
    assert "APPROVED" in formatted
    assert "REJECTED" not in formatted


def test_refuses_when_too_few_exemplars() -> None:
    """Two exemplars (below default min of 3) should yield a refusal message."""
    exemplars = [Exemplar("up", "r", "o", 1.0), Exemplar("down", "r2", "o2", 2.0)]
    result = asyncio.run(suggest_house_style(exemplars, min_exemplars=3))
    assert "Not enough" in result
    assert "need 3" in result


def test_empty_exemplars_refuses() -> None:
    result = asyncio.run(suggest_house_style([], min_exemplars=1))
    assert "Not enough" in result
