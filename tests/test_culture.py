"""Tests for the culture layer."""

from __future__ import annotations

import json
from pathlib import Path


from anthill.core.culture import Culture, load_culture, save_culture
from anthill.core.scout import build_system_prompt


def test_record_increments_count() -> None:
    c = Culture()
    c.record("translate")
    c.record("translate")
    c.record("explain")
    assert c.task_catalog == {"translate": 2, "explain": 1}


def test_known_task_types_are_hot_first() -> None:
    c = Culture(task_catalog={"a": 1, "b": 5, "c": 3})
    assert c.known_task_types() == ["b", "c", "a"]


def test_min_count_filter() -> None:
    c = Culture(task_catalog={"common": 10, "rare": 1})
    assert c.known_task_types(min_count=5) == ["common"]


def test_summarize_empty() -> None:
    assert "young nation" in Culture().summarize().lower()


def test_summarize_with_history() -> None:
    c = Culture(task_catalog={"translate": 10, "explain": 5})
    text = c.summarize()
    assert "15 tasks" in text
    assert "translate" in text


def test_persistence_roundtrip(tmp_path: Path) -> None:
    c = Culture(
        task_catalog={"translate": 3, "summarize": 1},
        house_style="Prefer terse.\nUse code examples.",
    )
    save_culture(c, tmp_path)
    loaded = load_culture(tmp_path)
    assert loaded.task_catalog == c.task_catalog
    assert loaded.house_style == c.house_style


def test_persistence_files_are_human_inspectable(tmp_path: Path) -> None:
    """Catalog is JSON, house style is markdown — both editable by hand."""
    c = Culture(task_catalog={"translate": 1}, house_style="be terse")
    save_culture(c, tmp_path)

    catalog = json.loads((tmp_path / "culture" / "catalog.json").read_text())
    assert catalog == {"translate": 1}

    style = (tmp_path / "culture" / "house_style.md").read_text()
    assert style == "be terse"


def test_scout_prompt_includes_known_types() -> None:
    prompt = build_system_prompt(known_task_types=["translate", "summarize"])
    assert "translate" in prompt
    assert "summarize" in prompt
    assert "prefer reusing" in prompt.lower()


def test_scout_prompt_handles_empty_vocabulary() -> None:
    prompt = build_system_prompt(known_task_types=None)
    assert "no prior task types" in prompt.lower()
    empty = build_system_prompt(known_task_types=[])
    assert "no prior task types" in empty.lower()
