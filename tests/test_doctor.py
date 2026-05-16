"""Tests for `anthill doctor`."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from anthill.cli.doctor import run_doctor
from anthill.cli.main import cli
from anthill.core.userconfig import (
    ModelEntry,
    UserConfig,
    save_config,
    save_secrets,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


def test_doctor_runs_without_crash(capsys: pytest.CaptureFixture[str]) -> None:
    code = run_doctor()
    captured = capsys.readouterr()
    assert "python" in captured.out
    assert "config" in captured.out
    # Empty install has misses but no fails.
    assert code == 0


def test_doctor_passes_when_model_configured(capsys: pytest.CaptureFixture[str]) -> None:
    save_config(
        UserConfig(
            default_model="x",
            models=[
                ModelEntry(
                    name="x",
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref="model.x",
                )
            ],
        )
    )
    save_secrets({"model.x": "sk-1234"})
    code = run_doctor()
    out = capsys.readouterr().out
    assert "x (deepseek/deepseek-chat)" in out
    assert code == 0


def test_doctor_fail_when_default_points_to_missing_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    save_config(UserConfig(default_model="missing", models=[]))
    code = run_doctor()
    out = capsys.readouterr().out
    # No models at all -> the 'miss' branch fires, not 'fail'.
    assert "no models configured" in out
    assert code == 0


def test_doctor_fail_when_default_missing_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    save_config(
        UserConfig(
            default_model="orphan",
            models=[
                ModelEntry(
                    name="orphan",
                    provider="deepseek",
                    model="deepseek-chat",
                    secret_ref="model.orphan",
                )
            ],
        )
    )
    # Deliberately not setting secrets.
    code = run_doctor()
    out = capsys.readouterr().out
    assert "no API key" in out
    assert code == 1


def test_cli_entrypoint_doctor() -> None:
    """Running `anthill doctor` via Click should work too."""
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    assert "python" in result.output
    # Empty config means misses; exit code 0.
    assert result.exit_code == 0
