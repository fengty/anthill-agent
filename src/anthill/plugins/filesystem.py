"""File-system plugins — read, list, write under a workspace root.

Plugins must stay within ANTHILL_PLUGIN_WORKSPACE (defaults to
~/.anthill/workspace). Any attempt to escape via .., absolute paths
outside the root, or symlinks is rejected. This is not a security
sandbox against a determined attacker, but it's the right default for
'agent did something I did not expect' protection.

Three plugins:
    file_read    Read text content of a file (size-capped).
    file_write   Create/overwrite a file with text content.
    file_list    List paths under a directory (one level by default).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from anthill.plugins.base import Plugin, PluginResult


def _workspace_root() -> Path:
    """Where files plugins are allowed to operate."""
    raw = os.getenv("ANTHILL_PLUGIN_WORKSPACE")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".anthill" / "workspace"


def resolve_in_workspace(rel_path: str) -> Path:
    """Public API: same as _resolve_safely, exported for other plugins."""
    return _resolve_safely(rel_path)


def _resolve_safely(rel_path: str) -> Path:
    """Return absolute path inside workspace, or raise if escape attempted."""
    root = _workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as e:
        raise PermissionError(
            f"Path {rel_path!r} escapes the plugin workspace at {root}."
        ) from e
    return candidate


class FileReadPlugin(Plugin):
    name = "file_read"
    description = "Read a UTF-8 file under the plugin workspace."

    async def call(self, *, path: str, max_bytes: int = 65536, **_: Any) -> PluginResult:
        try:
            abs_path = _resolve_safely(path)
        except PermissionError as e:
            return PluginResult(output=None, ok=False, error=str(e))
        if not abs_path.exists():
            return PluginResult(output=None, ok=False, error=f"no such file: {path}")
        if not abs_path.is_file():
            return PluginResult(output=None, ok=False, error=f"not a file: {path}")
        try:
            data = abs_path.read_bytes()[:max_bytes]
            text = data.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return PluginResult(output=None, ok=False, error=str(e))
        return PluginResult(
            output=text,
            metadata={
                "path": str(abs_path),
                "bytes_read": len(data),
                "truncated": abs_path.stat().st_size > max_bytes,
            },
        )


class FileWritePlugin(Plugin):
    name = "file_write"
    description = "Create or overwrite a file under the plugin workspace."

    async def call(self, *, path: str, content: str, **_: Any) -> PluginResult:
        try:
            abs_path = _resolve_safely(path)
        except PermissionError as e:
            return PluginResult(output=None, ok=False, error=str(e))
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            abs_path.write_text(content, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return PluginResult(output=None, ok=False, error=str(e))
        return PluginResult(output=str(abs_path), metadata={"bytes_written": len(content)})


class FileListPlugin(Plugin):
    name = "file_list"
    description = "List immediate children of a directory under the workspace."

    async def call(self, *, path: str = ".", **_: Any) -> PluginResult:
        try:
            abs_path = _resolve_safely(path)
        except PermissionError as e:
            return PluginResult(output=None, ok=False, error=str(e))
        if not abs_path.exists():
            return PluginResult(output=None, ok=False, error=f"no such dir: {path}")
        if not abs_path.is_dir():
            return PluginResult(output=None, ok=False, error=f"not a dir: {path}")
        children = []
        for child in sorted(abs_path.iterdir()):
            children.append({
                "name": child.name,
                "kind": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else None,
            })
        return PluginResult(output=children, metadata={"count": len(children)})
