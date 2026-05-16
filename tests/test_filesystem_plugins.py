"""Tests for filesystem plugins."""

from __future__ import annotations

import asyncio
from pathlib import Path


from anthill.plugins.filesystem import (
    FileListPlugin,
    FileReadPlugin,
    FileWritePlugin,
)


def test_write_then_read(workspace: Path) -> None:
    write_result = asyncio.run(FileWritePlugin().call(path="hello.txt", content="world"))
    assert write_result.ok
    read_result = asyncio.run(FileReadPlugin().call(path="hello.txt"))
    assert read_result.ok
    assert read_result.output == "world"


def test_read_missing_file(workspace: Path) -> None:
    result = asyncio.run(FileReadPlugin().call(path="nope.txt"))
    assert not result.ok
    assert "no such file" in result.error


def test_read_blocks_escape(workspace: Path) -> None:
    result = asyncio.run(FileReadPlugin().call(path="../../etc/passwd"))
    assert not result.ok
    assert "escapes" in result.error


def test_write_creates_intermediate_dirs(workspace: Path) -> None:
    result = asyncio.run(FileWritePlugin().call(path="a/b/c.txt", content="deep"))
    assert result.ok
    assert (workspace / "a" / "b" / "c.txt").read_text() == "deep"


def test_list_returns_children(workspace: Path) -> None:
    asyncio.run(FileWritePlugin().call(path="x.txt", content="x"))
    asyncio.run(FileWritePlugin().call(path="y.txt", content="y"))
    result = asyncio.run(FileListPlugin().call(path="."))
    assert result.ok
    names = [c["name"] for c in result.output]
    assert "x.txt" in names
    assert "y.txt" in names


def test_list_distinguishes_dir_from_file(workspace: Path) -> None:
    asyncio.run(FileWritePlugin().call(path="sub/file.txt", content="x"))
    result = asyncio.run(FileListPlugin().call(path="."))
    kinds = {c["name"]: c["kind"] for c in result.output}
    assert kinds["sub"] == "dir"
