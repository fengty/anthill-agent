"""0.1.29 — persistent USER.md / MEMORY.md files.

Both Claude Code (v2.1.x) and Hermes (Nous) converged on the same
two-file pattern in 2026:

  - **User-global profile** — what the agent knows about *you*
    (style preferences, languages, recurring requests, things
    you've explicitly asked it to remember). Survives nation
    switches. Plain text, human-editable.

  - **Per-nation knowledge** — what THIS nation has learned about
    the work it does (project facts, conventions, lessons,
    "this nation's job is X"). Tied to one nation; switching
    nations switches the file.

Anthill ships neither today. Pheromones learn silently. History
records but doesn't distill. Without a visible, editable, persistent
profile, "the more you use it the more it knows you" is a claim,
not a feature.

This module is the foundation:

  - Reads / writes both files with sane defaults on first access
  - Bounded size (caps at MAX_FILE_CHARS to keep the system prompt
    from blowing up)
  - Append helpers for `/remember` and `/remember-me`
  - `build_memory_block()` produces the string injected into every
    Scout + worker system prompt

The auto-write loop ("after each ask, decide what to remember")
lives in 0.1.30 — this is purely the file + injection plumbing.
"""

from __future__ import annotations

import time
from pathlib import Path


USER_MD_FILENAME = "USER.md"
NATION_MEMORY_FILENAME = "MEMORY.md"

# Hard cap per file to keep the injected system prompt under control.
# Claude Code uses ~200 lines; we cap by char count (~8 KB) which is
# roughly equivalent and stricter about pathological line lengths.
# Over-cap → memory hygiene (0.1.34) consolidates / archives.
MAX_FILE_CHARS = 8000

# What goes in a freshly-created file, so the user sees something
# real (not an empty file) and knows what to put in.
_USER_MD_TEMPLATE = """\
# About the user

Anthill writes here automatically as it learns about you, and you can
edit this file directly. Anything in this file is injected into every
prompt the nation works on, so keep it concrete and stable.

## Preferences

(empty — fill in or run `anthill` and use `/remember-me <thing>`)

## Working style

(empty)

## Languages / locales

(empty)
"""

_NATION_MEMORY_TEMPLATE = """\
# Nation memory: {nation}

What this nation has learned about the work it does. Auto-grown after
successful tasks; you can also edit directly or use `/remember <line>`.

## What this nation is for

(empty — describe in one line what you mostly ask this nation to do)

## Lessons

(empty)

## Conventions

(empty)
"""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def user_md_path(home: Path) -> Path:
    """Where the global user profile lives."""
    return home / USER_MD_FILENAME


def nation_memory_path(nation_dir: Path) -> Path:
    """Where THIS nation's persistent memory lives."""
    return nation_dir / NATION_MEMORY_FILENAME


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def read_user_md(home: Path) -> str:
    """Return contents; empty string when the file doesn't exist.

    Bootstrapping is opt-in via ``ensure_user_md`` — we deliberately
    DON'T create the file on every read so headless / CI environments
    don't sprinkle empty USER.md everywhere.
    """
    path = user_md_path(home)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def write_user_md(home: Path, text: str) -> None:
    """Atomic write with size cap honored at the call site."""
    home.mkdir(parents=True, exist_ok=True)
    path = user_md_path(home)
    _atomic_write(path, text)


def read_nation_memory(nation_dir: Path) -> str:
    path = nation_memory_path(nation_dir)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def write_nation_memory(nation_dir: Path, text: str) -> None:
    nation_dir.mkdir(parents=True, exist_ok=True)
    path = nation_memory_path(nation_dir)
    _atomic_write(path, text)


def ensure_user_md(home: Path) -> Path:
    """Create USER.md with the template if it doesn't exist. Returns
    the path either way so callers can chain into an editor."""
    path = user_md_path(home)
    if not path.exists():
        write_user_md(home, _USER_MD_TEMPLATE)
    return path


def ensure_nation_memory(nation_dir: Path, nation_name: str) -> Path:
    path = nation_memory_path(nation_dir)
    if not path.exists():
        write_nation_memory(
            nation_dir,
            _NATION_MEMORY_TEMPLATE.format(nation=nation_name),
        )
    return path


# ---------------------------------------------------------------------------
# Append helpers (used by /remember / /remember-me + the future auto-memory)
# ---------------------------------------------------------------------------


def append_user_md(home: Path, line: str, *, section: str = "Preferences") -> bool:
    """Append a single dated line under ``## <section>``. Creates the
    file with the template if missing. Returns True on success.

    Lines look like:
        - 2026-05-17  prefers concise answers in Chinese
    Hyphen prefix + date + body — the human can read it as a list and
    the agent can scan it.
    """
    ensure_user_md(home)
    return _append_under_section(user_md_path(home), section, line)


def append_nation_memory(
    nation_dir: Path,
    line: str,
    nation_name: str = "",
    *,
    section: str = "Lessons",
) -> bool:
    ensure_nation_memory(nation_dir, nation_name or nation_dir.name)
    return _append_under_section(nation_memory_path(nation_dir), section, line)


# ---------------------------------------------------------------------------
# Injection — the system-prompt block
# ---------------------------------------------------------------------------


def build_memory_block(user_md: str, nation_md: str) -> str:
    """Build the text injected into every Scout + worker system prompt.

    Two clearly-delimited sections so the model knows what's about
    the user vs what's about the project. Empty when both sources
    are empty — callers should treat the empty string as "nothing
    to inject" and skip the section entirely.
    """
    pieces: list[str] = []
    if user_md.strip():
        pieces.append(
            "[about the user — apply these preferences to every answer]\n"
            + user_md.strip()
        )
    if nation_md.strip():
        pieces.append(
            "[nation memory — what this nation has learned]\n" + nation_md.strip()
        )
    return "\n\n".join(pieces)


def line_count(text: str) -> int:
    """Used by the splash card to show "N memory lines"."""
    return sum(1 for line in text.splitlines() if line.strip())


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling .tmp file then rename — same pattern the
    plan cache and inflight stores use. Survives partial-write
    corruption."""
    if len(text) > MAX_FILE_CHARS:
        # Soft truncation at file boundary. The 0.1.34 memory
        # hygiene patch handles intelligent consolidation; this
        # is the dumb-but-safe fallback so a runaway auto-memory
        # write can't grow the file past the system-prompt budget.
        text = (
            text[: MAX_FILE_CHARS - 200]
            + "\n\n<!-- truncated by anthill memory hygiene fallback -->\n"
        )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _append_under_section(path: Path, section: str, line: str) -> bool:
    """Insert ``- <YYYY-MM-DD>  <line>`` under ``## <section>``.

    If the section doesn't exist, append it at the bottom of the file.
    Empty-line placeholders like "(empty)" are removed when the first
    real line goes in, so the section stops looking unloved.
    """
    line = line.strip()
    if not line:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    date = time.strftime("%Y-%m-%d")
    entry = f"- {date}  {line}"

    header = f"## {section}"
    if header in text:
        # Insert entry right after the header line, replacing any
        # "(empty)" placeholder we find immediately under it.
        lines = text.splitlines()
        out: list[str] = []
        inserted = False
        for i, ln in enumerate(lines):
            out.append(ln)
            if inserted or ln.strip() != header:
                continue
            # Look ahead to drop a "(empty)" line if present.
            if i + 1 < len(lines) and lines[i + 1].strip() == "(empty)":
                lines[i + 1] = entry
                continue
            out.append("")
            out.append(entry)
            inserted = True
        if not inserted:
            # Header was the last line in the file.
            out.extend(["", entry])
        new_text = "\n".join(out)
        if not new_text.endswith("\n"):
            new_text += "\n"
    else:
        # No section yet — append a new one.
        new_text = text.rstrip() + f"\n\n{header}\n\n{entry}\n"

    _atomic_write(path, new_text)
    return True
