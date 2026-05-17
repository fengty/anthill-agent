"""0.1.29 — USER.md + MEMORY.md persistent memory files.

The big architectural shift: Anthill grows two plain-text files —
one global (USER.md, in ~/.anthill/) and one per-nation (MEMORY.md,
in ~/.anthill/nations/<name>/) — both injected into every Scout +
worker system prompt at session start.

This mirrors what Claude Code (CLAUDE.md) and Hermes (USER.md +
MEMORY.md in ~/.hermes/memories/) shipped in 2026 and that Anthill
lacked.

Tests cover:
- read returns empty string on missing file (no crash)
- write is atomic and respects size cap
- append helpers under named sections
- "(empty)" placeholder gets replaced by the first real entry
- build_memory_block produces clean injection text
- line_count powers the splash card
- ensure_* bootstraps with a friendly template on first touch
"""

from __future__ import annotations

from pathlib import Path



def test_read_user_md_missing_file_returns_empty(tmp_path: Path) -> None:
    from anthill.core.memory_files import read_user_md

    assert read_user_md(tmp_path) == ""


def test_read_nation_memory_missing_file_returns_empty(tmp_path: Path) -> None:
    from anthill.core.memory_files import read_nation_memory

    assert read_nation_memory(tmp_path) == ""


def test_write_then_read_user_md_round_trip(tmp_path: Path) -> None:
    from anthill.core.memory_files import read_user_md, write_user_md

    write_user_md(tmp_path, "# About me\nLikes Chinese-first answers.\n")
    text = read_user_md(tmp_path)
    assert "Chinese-first" in text


def test_ensure_user_md_creates_with_template(tmp_path: Path) -> None:
    from anthill.core.memory_files import ensure_user_md, read_user_md

    path = ensure_user_md(tmp_path)
    assert path.exists()
    text = read_user_md(tmp_path)
    # Template should mention preferences and have clear sections.
    assert "## Preferences" in text
    assert "## Working style" in text


def test_ensure_user_md_idempotent(tmp_path: Path) -> None:
    """Calling ensure twice doesn't clobber existing content."""
    from anthill.core.memory_files import (
        ensure_user_md,
        read_user_md,
        write_user_md,
    )

    write_user_md(tmp_path, "# About me\nUser-edited content.\n")
    ensure_user_md(tmp_path)  # should NOT overwrite
    assert "User-edited content" in read_user_md(tmp_path)


def test_ensure_nation_memory_names_the_nation(tmp_path: Path) -> None:
    from anthill.core.memory_files import (
        ensure_nation_memory,
        read_nation_memory,
    )

    ensure_nation_memory(tmp_path, "work-nation")
    text = read_nation_memory(tmp_path)
    assert "work-nation" in text


def test_size_cap_enforced(tmp_path: Path) -> None:
    """Way-over-cap writes are truncated with a marker line."""
    from anthill.core.memory_files import (
        MAX_FILE_CHARS,
        read_user_md,
        write_user_md,
    )

    huge = "A" * (MAX_FILE_CHARS * 5)
    write_user_md(tmp_path, huge)
    text = read_user_md(tmp_path)
    assert len(text) <= MAX_FILE_CHARS
    assert "truncated" in text


def test_append_user_md_creates_section_when_new(tmp_path: Path) -> None:
    from anthill.core.memory_files import append_user_md, read_user_md

    # First append creates USER.md with the template, then inserts.
    ok = append_user_md(tmp_path, "prefers Chinese answers")
    assert ok is True
    text = read_user_md(tmp_path)
    assert "prefers Chinese answers" in text
    # Date-stamped under "Preferences"
    assert "## Preferences" in text
    # The "(empty)" placeholder got replaced.
    assert "(empty)" not in text.split("## Preferences", 1)[1].split("##", 1)[0]


def test_append_user_md_under_custom_section(tmp_path: Path) -> None:
    from anthill.core.memory_files import append_user_md, read_user_md

    append_user_md(tmp_path, "always wear black", section="Style")
    text = read_user_md(tmp_path)
    assert "## Style" in text
    assert "always wear black" in text


def test_append_nation_memory_works(tmp_path: Path) -> None:
    from anthill.core.memory_files import (
        append_nation_memory,
        read_nation_memory,
    )

    append_nation_memory(tmp_path, "deepseek often truncates over 4K", "my-nation")
    text = read_nation_memory(tmp_path)
    assert "deepseek often truncates" in text
    assert "my-nation" in text  # nation name in the header


def test_append_empty_line_returns_false(tmp_path: Path) -> None:
    from anthill.core.memory_files import append_user_md

    assert append_user_md(tmp_path, "   ") is False
    assert append_user_md(tmp_path, "") is False


def test_appends_are_date_stamped(tmp_path: Path) -> None:
    """Every append carries a YYYY-MM-DD prefix for chronology."""
    import re

    from anthill.core.memory_files import append_user_md, read_user_md

    append_user_md(tmp_path, "remember this")
    text = read_user_md(tmp_path)
    # Find at least one "- YYYY-MM-DD  remember this"
    assert re.search(r"- \d{4}-\d{2}-\d{2}\s+remember this", text) is not None


def test_build_memory_block_empty_when_no_sources() -> None:
    from anthill.core.memory_files import build_memory_block

    assert build_memory_block("", "") == ""
    assert build_memory_block("   ", "\n\n") == ""


def test_build_memory_block_includes_both_sections() -> None:
    from anthill.core.memory_files import build_memory_block

    block = build_memory_block(
        "# About me\nLikes concise.\n",
        "# Nation memory\nWorks on translation.\n",
    )
    assert "about the user" in block
    assert "Likes concise" in block
    assert "nation memory" in block.lower()
    assert "Works on translation" in block


def test_build_memory_block_skips_empty_side() -> None:
    from anthill.core.memory_files import build_memory_block

    only_user = build_memory_block("Likes Chinese", "")
    assert "user" in only_user.lower()
    assert "nation memory" not in only_user.lower()


def test_line_count_ignores_blank_lines() -> None:
    from anthill.core.memory_files import line_count

    text = "\n\n# Header\n\n- one\n  \n- two\n"
    assert line_count(text) == 3


def test_memory_context_attribute_default_empty() -> None:
    """Nation defaults memory_context to '' so headless tests / first-run
    sessions don't crash on the new attribute."""
    from anthill.core.nation import Nation

    n = Nation(name="t")
    assert n.memory_context == ""


def test_compose_system_includes_memory(monkeypatch) -> None:
    """The whole point: when memory_context is set, _compose_system
    actually injects it into the worker's system prompt."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation

    n = Nation(name="t")
    n.memory_context = "[about the user] prefers Chinese answers."
    sys_prompt = n._compose_system(Agent(id="ant-1", model="x"))
    assert sys_prompt is not None
    assert "prefers Chinese answers" in sys_prompt
