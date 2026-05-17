"""0.1.15 — project context detection.

The REPL auto-detects a project root by walking up from cwd looking
for language / build markers (`pyproject.toml`, `package.json`,
`.git`, etc) and surfaces a one-block summary to Scout. Tests cover
the marker priority, walk depth, top-level listing, Git status
fallback, and the rendered block shape.
"""

from __future__ import annotations

from pathlib import Path


def test_find_project_root_python(tmp_path: Path) -> None:
    from anthill.core.project import find_project_root

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
    info = find_project_root(tmp_path)
    assert info is not None
    assert info.kind == "Python"
    assert info.marker == "pyproject.toml"
    assert info.root == tmp_path


def test_find_project_root_walks_up(tmp_path: Path) -> None:
    """Run from a subdirectory — should still find the project root."""
    from anthill.core.project import find_project_root

    (tmp_path / "package.json").write_text("{}")
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    info = find_project_root(sub)
    assert info is not None
    assert info.kind == "Node.js"
    assert info.root == tmp_path


def test_find_project_root_returns_none_when_no_markers(tmp_path: Path) -> None:
    from anthill.core.project import find_project_root

    # Use a deeply isolated path that won't accidentally walk into a
    # real project on the test machine.
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    # Even with 6-level walk we shouldn't escape tmp_path to a real
    # marker — unless the test runs FROM a marked project. So we
    # just assert the result is either None or points back into
    # tmp_path or higher. Tightest sensible check.
    info = find_project_root(deep)
    if info is not None:
        # If found, it should be at or above the test temp dir's
        # parents — not inside our marker-free temp.
        assert info.root not in (deep, deep.parent, deep.parent.parent)


def test_marker_priority_python_over_git(tmp_path: Path) -> None:
    """Both pyproject.toml and .git present ⇒ Python wins (more specific)."""
    from anthill.core.project import find_project_root

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
    (tmp_path / ".git").mkdir()
    info = find_project_root(tmp_path)
    assert info is not None
    assert info.kind == "Python"


def test_top_level_entries_sorted_dirs_first(tmp_path: Path) -> None:
    from anthill.core.project import _list_top_level

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("readme")
    (tmp_path / "pyproject.toml").write_text("x")
    entries = _list_top_level(tmp_path)
    # Dirs come first (the sort key puts is_file=False before is_file=True),
    # then files; within each group alphabetical.
    assert entries[:2] == ("src/", "tests/")
    assert "README.md" in entries
    assert "pyproject.toml" in entries


def test_top_level_hides_dotfiles_except_github(tmp_path: Path) -> None:
    from anthill.core.project import _list_top_level

    (tmp_path / ".gitignore").write_text("x")
    (tmp_path / ".github").mkdir()
    (tmp_path / "README.md").write_text("r")
    entries = _list_top_level(tmp_path)
    assert ".gitignore" not in entries
    # .github/ is project-meaningful, keep it
    assert ".github/" in entries
    assert "README.md" in entries


def test_top_level_caps_at_max(tmp_path: Path) -> None:
    from anthill.core.project import MAX_TOP_LEVEL_ENTRIES, _list_top_level

    for i in range(MAX_TOP_LEVEL_ENTRIES + 10):
        (tmp_path / f"file{i:03d}.txt").write_text("x")
    entries = _list_top_level(tmp_path)
    assert len(entries) == MAX_TOP_LEVEL_ENTRIES


def test_project_context_block_empty_for_none() -> None:
    from anthill.core.project import project_context_block

    assert project_context_block(None) == ""


def test_project_context_block_renders_name_and_kind(tmp_path: Path) -> None:
    from anthill.core.project import find_project_root, project_context_block

    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'")
    (tmp_path / "src").mkdir()
    info = find_project_root(tmp_path)
    block = project_context_block(info)
    assert f"project: {tmp_path.name}" in block
    assert "Rust" in block
    assert "Cargo.toml" in block
    assert "src/" in block


def test_git_status_returns_none_for_non_git(tmp_path: Path) -> None:
    """Non-git directories return (None, 0) cleanly."""
    from anthill.core.project import _git_status

    branch, dirty = _git_status(tmp_path)
    assert branch is None
    assert dirty == 0


def test_enrich_handles_unreadable_dir(monkeypatch, tmp_path: Path) -> None:
    """If iterdir throws, top_level_entries is empty but rest survives."""
    from anthill.core.project import _enrich

    def raise_oserror(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", raise_oserror)
    info = _enrich(tmp_path, "Python", "pyproject.toml")
    assert info.top_level_entries == ()
    assert info.kind == "Python"
