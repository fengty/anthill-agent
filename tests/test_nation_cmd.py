"""Tests for `anthill nation` subcommands."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from anthill.cli.nation_cmd import nation as nation_group
from anthill.core.persistence import load_nation, nation_dir


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


def test_list_empty_shows_hint() -> None:
    result = CliRunner().invoke(nation_group, ["list"])
    assert result.exit_code == 0
    assert "No nations yet" in result.output


def test_create_then_list() -> None:
    runner = CliRunner()
    create_result = runner.invoke(nation_group, ["create", "kingdom"])
    assert create_result.exit_code == 0
    assert "Founded 'kingdom'" in create_result.output

    list_result = runner.invoke(nation_group, ["list"])
    assert list_result.exit_code == 0
    assert "kingdom" in list_result.output


def test_create_with_citizens() -> None:
    runner = CliRunner()
    result = runner.invoke(nation_group, ["create", "demo", "--citizens", "3"])
    assert result.exit_code == 0
    # Nation should have 3 citizens persisted.
    from anthill.config import AnthillConfig
    nation = load_nation("demo", AnthillConfig.load().home)
    assert nation is not None
    assert len(nation.agents) == 3


def test_create_refuses_existing_name(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(nation_group, ["create", "x"])
    second = runner.invoke(nation_group, ["create", "x"])
    assert second.exit_code != 0
    assert "already exists" in second.output


def test_show_nonexistent_returns_error() -> None:
    result = CliRunner().invoke(nation_group, ["show", "ghost"])
    assert result.exit_code != 0
    assert "No nation named" in result.output


def test_show_existing_displays_summary() -> None:
    runner = CliRunner()
    runner.invoke(nation_group, ["create", "kingdom"])
    result = runner.invoke(nation_group, ["show", "kingdom"])
    assert result.exit_code == 0
    assert "citizens" in result.output


def test_switch_sets_current_pointer() -> None:
    runner = CliRunner()
    runner.invoke(nation_group, ["create", "a"])
    runner.invoke(nation_group, ["create", "b"])
    result = runner.invoke(nation_group, ["switch", "b"])
    assert result.exit_code == 0

    from anthill.config import AnthillConfig
    pointer = AnthillConfig.load().home / "current_nation"
    assert pointer.read_text() == "b"


def test_switch_to_nonexistent_errors() -> None:
    result = CliRunner().invoke(nation_group, ["switch", "ghost"])
    assert result.exit_code != 0


def test_rename_moves_directory_and_state() -> None:
    runner = CliRunner()
    runner.invoke(nation_group, ["create", "old"])
    result = runner.invoke(nation_group, ["rename", "old", "new"])
    assert result.exit_code == 0

    from anthill.config import AnthillConfig
    config = AnthillConfig.load()
    assert not nation_dir(config.home, "old").exists()
    assert nation_dir(config.home, "new").exists()
    renamed = load_nation("new", config.home)
    assert renamed is not None
    assert renamed.name == "new"


def test_remove_with_yes_deletes_dir() -> None:
    runner = CliRunner()
    runner.invoke(nation_group, ["create", "doomed"])
    result = runner.invoke(nation_group, ["remove", "doomed", "--yes"])
    assert result.exit_code == 0

    from anthill.config import AnthillConfig
    assert not nation_dir(AnthillConfig.load().home, "doomed").exists()
