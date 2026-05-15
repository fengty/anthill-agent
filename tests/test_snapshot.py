"""Tests for nation snapshot export/import."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anthill.core.snapshot import export_nation, import_nation


def _make_minimal_nation(root: Path, name: str = "demo") -> Path:
    nation = root / name
    nation.mkdir(parents=True)
    (nation / "agents.json").write_text(
        json.dumps([{"id": "ant-1", "model": "deepseek-chat", "persona": None, "private_memory": {}}])
    )
    (nation / "pheromones.json").write_text(json.dumps([]))
    culture = nation / "culture"
    culture.mkdir()
    (culture / "catalog.json").write_text(json.dumps({"explain": 5, "translate": 3}))
    (culture / "house_style.md").write_text("be terse")
    (nation / "history.jsonl").write_text(
        '{"id": "x", "timestamp": 1.0, "request": "r", "plan": [], "outcomes": []}\n'
    )
    return nation


def test_export_creates_archive(tmp_path: Path) -> None:
    nation = _make_minimal_nation(tmp_path / "src")
    output = tmp_path / "snap.tar.gz"
    manifest = export_nation(nation, output)
    assert output.exists()
    assert manifest.citizen_count == 1
    assert manifest.vocabulary_size == 2
    assert manifest.history_entries == 1


def test_export_then_import_roundtrip(tmp_path: Path) -> None:
    nation = _make_minimal_nation(tmp_path / "src")
    output = tmp_path / "snap.tar.gz"
    export_nation(nation, output)

    target = tmp_path / "target"
    target.mkdir()
    manifest = import_nation(output, target)
    assert manifest.nation_name == "demo"
    assert manifest.citizen_count == 1

    restored = target / "demo"
    assert (restored / "agents.json").exists()
    assert (restored / "culture" / "house_style.md").read_text() == "be terse"


def test_import_refuses_to_overwrite(tmp_path: Path) -> None:
    nation = _make_minimal_nation(tmp_path / "src")
    archive = tmp_path / "snap.tar.gz"
    export_nation(nation, archive)

    target = tmp_path / "target"
    _make_minimal_nation(target)
    with pytest.raises(FileExistsError):
        import_nation(archive, target)


def test_export_raises_if_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        export_nation(tmp_path / "nope", tmp_path / "out.tar.gz")
