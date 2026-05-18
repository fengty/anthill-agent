"""0.1.15 — Project context: bind the REPL to the current working directory.

When the user runs ``anthill`` inside a project directory (Git repo,
Node package, Python project, etc), surface the project's identity
and a lightweight file-tree summary as context for Scout. The model
sees something like:

    [project: anthill-agent — Python (pyproject.toml)]
    Top-level files (15): README.md, pyproject.toml, src/, tests/, ...
    Branch: main · 3 modified file(s) staged

The goal is "Scout knows what kind of place this is" without paying
the cost of full repo embedding (that's a different lane — see
docs/comparison.md "out of scope").

Detection is heuristic and best-effort: missing markers / dirty repos
/ submodules are all fine, they just shrink the block. The whole
module is pure-stdlib so it never breaks the REPL on minimal hosts.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


# Ordered: more specific markers first so a Python project inside a
# monorepo still detects as Python rather than Generic.
PROJECT_MARKERS: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", "Python"),
    ("setup.py", "Python"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("package.json", "Node.js"),
    ("Gemfile", "Ruby"),
    ("composer.json", "PHP"),
    ("pom.xml", "Java (Maven)"),
    ("build.gradle", "Java (Gradle)"),
    ("CMakeLists.txt", "C/C++ (CMake)"),
    ("Makefile", "Make"),
    (".git", "Git repo"),
)

# How far up to walk looking for a project root. 6 is enough to catch
# the "ran from a subdirectory" case without scanning the whole disk.
MAX_PARENT_WALK = 6

# Cap on file/dir entries listed in the summary so the block stays
# under a few KB even for sprawling monorepos.
MAX_TOP_LEVEL_ENTRIES = 25


@dataclass
class ProjectInfo:
    """A summary of what we found at / above the current working dir."""

    root: Path
    name: str           # leaf directory name — what humans call the project
    kind: str           # detected language / framework, "Generic" as fallback
    marker: str         # the file/dir that triggered detection
    git_branch: str | None = None
    git_dirty_count: int = 0      # 0 when clean / no git
    top_level_entries: tuple[str, ...] = ()


def find_project_root(start: Path | None = None) -> ProjectInfo | None:
    """Walk up from ``start`` looking for a project marker. None if not found.

    Resolution order matches PROJECT_MARKERS so the most specific
    marker wins (e.g. `pyproject.toml` beats `.git` when both exist).
    """
    start = (start or Path.cwd()).resolve()
    parents = [start, *start.parents]
    for candidate in parents[:MAX_PARENT_WALK]:
        for marker, kind in PROJECT_MARKERS:
            marker_path = candidate / marker
            if marker_path.exists():
                return _enrich(candidate, kind, marker)
    return None


def _enrich(root: Path, kind: str, marker: str) -> ProjectInfo:
    """Layer Git info + top-level listing onto the base ProjectInfo."""
    info = ProjectInfo(root=root, name=root.name, kind=kind, marker=marker)
    info.git_branch, info.git_dirty_count = _git_status(root)
    info.top_level_entries = _list_top_level(root)
    return info


def _git_status(root: Path) -> tuple[str | None, int]:
    """Best-effort current branch + dirty-file count.

    Silently returns (None, 0) when git isn't available, when ``root``
    isn't a git repo, or when the call errors out for any reason. We
    never want project-context inspection to crash the REPL.
    """
    if not (root / ".git").exists():
        return None, 0
    try:
        branch = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if branch.returncode != 0:
            return None, 0
        branch_name = branch.stdout.strip() or None
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        dirty = (
            sum(1 for line in status.stdout.splitlines() if line.strip())
            if status.returncode == 0
            else 0
        )
        return branch_name, dirty
    except (OSError, subprocess.TimeoutExpired):
        return None, 0


def _list_top_level(root: Path) -> tuple[str, ...]:
    """Top-level files and directories, sorted, with trailing / on dirs.

    Dotfiles hidden (one exception: `.github` is project-meaningful so
    we surface it). Caps at MAX_TOP_LEVEL_ENTRIES so a monorepo doesn't
    blow up the block.
    """
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name))
    except OSError:
        return ()
    out: list[str] = []
    for child in entries:
        if child.name.startswith(".") and child.name != ".github":
            continue
        suffix = "/" if child.is_dir() else ""
        out.append(f"{child.name}{suffix}")
        if len(out) >= MAX_TOP_LEVEL_ENTRIES:
            break
    return tuple(out)


def project_context_block(info: ProjectInfo | None) -> str:
    """Render a ProjectInfo as a Scout-readable context block.

    Empty string when info is None so callers can ``"\\n".join(...)`` it
    unconditionally with other context blocks.
    """
    if info is None:
        return ""
    lines = [
        f"[project: {info.name} — {info.kind} ({info.marker})]"
    ]
    if info.top_level_entries:
        listed = ", ".join(info.top_level_entries)
        lines.append(
            f"Top-level entries ({len(info.top_level_entries)}): {listed}"
        )
    if info.git_branch is not None:
        if info.git_dirty_count > 0:
            lines.append(
                f"Git: branch {info.git_branch} · "
                f"{info.git_dirty_count} modified file(s)"
            )
        else:
            lines.append(f"Git: branch {info.git_branch} · clean")
    return "\n".join(lines)


# 0.1.33 — heuristic: does the request actually mention the project?
#
# Background: 0.1.15 made Anthill always inject project context when
# launched from a detected project root. That helps for "fix this
# bug" / "refactor @file.py" but actively POISONS general queries.
# Real example a user hit:
#
#   $ cd anthill-agent
#   $ anthill
#   » 我希望找个 AI 项目研究下
#
# Scout saw "[project: anthill-agent — Python]" in its system prompt
# and answered with five fictional AI subprojects "for the agent's
# scaling API" — confabulated entirely. The user's question was
# external, the context was internal, and Scout fused them.
#
# This function gates the injection: only when the request looks
# like it's about THIS project does the block go in. Otherwise the
# nation operates project-unaware for that ask. Users can override
# with `/project on` (force) or `/project off` (disable for session).

_PROJECT_RELEVANCE_MARKERS = (
    # English direct references
    "this project", "this repo", "this codebase", "this code",
    "the project", "the repo", "the codebase",
    "my project", "my code", "my repo",
    "in this", "in here", "in our",
    # Action verbs that strongly imply the local project
    "refactor", "review my", "review the", "review this",
    "fix the bug", "the bug in", "the file",
    "add a test", "add a feature", "implement in",
    # File / path / structure references
    "src/", "tests/", "package.json", "pyproject.toml",
    ".py", ".ts", ".tsx", ".js", ".go", ".rs",
    "/file", "/dir",
    # Chinese direct references
    "这个项目", "这个代码", "这个仓库", "这份代码",
    "我的项目", "我的代码", "我的仓库",
    "本项目", "本代码",
    "项目里", "项目中", "项目下",
    "代码里", "代码中", "代码库",
    "这里的", "这边的",
    "改一下", "修一下", "看下我的", "审阅",
)


def is_project_relevant_request(request: str) -> bool:
    """Should the project context block be injected for THIS ask?

    True for requests that name the local project / refer to "this
    code" / mention file paths / use the local-action verbs. False
    for purely external questions like "recommend an AI project" or
    "what's the weather."

    The `@file` syntax is handled separately at the REPL layer — if
    the user attached files explicitly, the attachment block speaks
    for itself; project metadata still helps Scout understand the
    surrounding shape, so `@`-tokens force-enable the injection.
    """
    if not request:
        return False
    lower = request.lower()
    if "@" in request:
        # Attachment syntax — almost certainly project-relevant.
        return True
    return any(marker in lower for marker in _PROJECT_RELEVANCE_MARKERS)
