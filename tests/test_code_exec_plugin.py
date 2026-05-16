"""Tests for code execution plugin."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anthill.plugins.code_exec import CodeExecPlugin


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHILL_PLUGIN_CODE_EXEC_ENABLED", raising=False)
    result = asyncio.run(CodeExecPlugin().call(code="print('hi')"))
    assert not result.ok
    assert "disabled" in result.error


def test_empty_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_PLUGIN_CODE_EXEC_ENABLED", "1")
    result = asyncio.run(CodeExecPlugin().call(code="   "))
    assert not result.ok
    assert "empty" in result.error


def test_simple_print(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHILL_PLUGIN_CODE_EXEC_ENABLED", "1")
    monkeypatch.setenv("ANTHILL_PLUGIN_WORKSPACE", str(tmp_path))
    result = asyncio.run(CodeExecPlugin().call(code="print(2+2)"))
    assert result.ok
    assert "4" in result.output["stdout"]
    assert result.output["exit_code"] == 0


def test_nonzero_exit_marks_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHILL_PLUGIN_CODE_EXEC_ENABLED", "1")
    monkeypatch.setenv("ANTHILL_PLUGIN_WORKSPACE", str(tmp_path))
    result = asyncio.run(CodeExecPlugin().call(code="import sys; sys.exit(3)"))
    assert not result.ok
    assert result.output["exit_code"] == 3


def test_stderr_captured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHILL_PLUGIN_CODE_EXEC_ENABLED", "1")
    monkeypatch.setenv("ANTHILL_PLUGIN_WORKSPACE", str(tmp_path))
    result = asyncio.run(
        CodeExecPlugin().call(code="import sys; print('oops', file=sys.stderr); sys.exit(1)")
    )
    assert "oops" in result.output["stderr"]


def test_timeout_kills_runaway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHILL_PLUGIN_CODE_EXEC_ENABLED", "1")
    monkeypatch.setenv("ANTHILL_PLUGIN_WORKSPACE", str(tmp_path))
    code = "import time; time.sleep(10)"
    result = asyncio.run(CodeExecPlugin().call(code=code, timeout=0.5))
    assert not result.ok
    assert "timeout" in result.error
