"""0.1.34 — memory hygiene tests.

Dedup near-duplicate lines, archive overflow when a section grows
past MAX_LINES_PER_SECTION. Mirrors Claude Code's 200-line cap + cron
rotation pattern and Hermes's "consolidate when capacity exceeded"
policy.

Tests cover:
- needs_hygiene fast-check
- bullet parser preserves non-bullet content
- dedup keeps newest of a near-duplicate cluster
- overflow → oldest go to archive
- empty / unchanged file → empty report, no write
- consolidate_user_md / consolidate_nation_memory wrappers
- archive file is append-only across runs
"""

from __future__ import annotations

from pathlib import Path


# --- needs_hygiene ----------------------------------------------------------


def test_needs_hygiene_false_for_empty() -> None:
    from anthill.core.memory_hygiene import needs_hygiene
    assert needs_hygiene("") is False


def test_needs_hygiene_false_for_small_clean_file() -> None:
    from anthill.core.memory_hygiene import needs_hygiene
    text = """## Preferences

- 2026-05-17  prefers concise
- 2026-05-18  prefers Chinese
"""
    assert needs_hygiene(text) is False


def test_needs_hygiene_true_when_section_overflows() -> None:
    from anthill.core.memory_hygiene import MAX_LINES_PER_SECTION, needs_hygiene
    bullets = "\n".join(
        f"- 2026-05-{i:02d}  preference number {i}"
        for i in range(1, MAX_LINES_PER_SECTION + 5)
    )
    text = f"## Preferences\n\n{bullets}\n"
    assert needs_hygiene(text) is True


def test_needs_hygiene_true_near_file_cap() -> None:
    from anthill.core.memory_files import MAX_FILE_CHARS
    from anthill.core.memory_hygiene import needs_hygiene
    text = "# header\n\n" + "x " * (int(MAX_FILE_CHARS * 0.8) // 2)
    assert needs_hygiene(text) is True


# --- dedup ------------------------------------------------------------------


def test_consolidate_drops_near_duplicate_lines() -> None:
    from anthill.core.memory_hygiene import consolidate_text
    text = """## Preferences

- 2026-05-01  prefers concise answers
- 2026-05-15  prefers concise answers
- 2026-05-18  loves Chinese-first
"""
    new_text, archive, report = consolidate_text(text)
    assert report.deduped == 1
    assert archive == ""
    # The NEWER duplicate is kept.
    assert "2026-05-15" in new_text
    assert "2026-05-01" not in new_text
    assert "loves Chinese-first" in new_text


def test_consolidate_keeps_distinct_lines() -> None:
    from anthill.core.memory_hygiene import consolidate_text
    text = """## Preferences

- 2026-05-15  prefers concise
- 2026-05-16  works on multi-agent systems
- 2026-05-17  based in Tokyo
"""
    _new, archive, report = consolidate_text(text)
    assert report.deduped == 0
    assert archive == ""


# --- overflow archive -------------------------------------------------------


def test_consolidate_archives_oldest_when_section_overflows() -> None:
    from anthill.core.memory_hygiene import MAX_LINES_PER_SECTION, consolidate_text
    lines = "\n".join(
        f"- 2026-05-{i:02d}  unique preference number {i}"
        for i in range(1, MAX_LINES_PER_SECTION + 6)
    )
    text = f"## Preferences\n\n{lines}\n"
    new_text, archive, report = consolidate_text(text)
    assert report.archived == 5
    # Archive contains the oldest 5.
    assert "2026-05-01" in archive
    assert "2026-05-05" in archive
    # Active text has the newest.
    last = f"unique preference number {MAX_LINES_PER_SECTION + 5}"
    assert last in new_text


def test_consolidate_no_archive_for_under_cap() -> None:
    from anthill.core.memory_hygiene import consolidate_text
    text = """## Preferences

- 2026-05-01  one
- 2026-05-02  two
- 2026-05-03  three
"""
    _new, archive, report = consolidate_text(text)
    assert archive == ""
    assert report.archived == 0


# --- preservation ----------------------------------------------------------


def test_consolidate_preserves_section_headers_and_prose() -> None:
    """Non-bullet content like prose paragraphs and HTML comments must
    survive — we're deduping bullets, not the whole markdown."""
    from anthill.core.memory_hygiene import consolidate_text
    text = """## Preferences

A line of prose explaining the section.
<!-- maintainer note -->

- 2026-05-15  same line
- 2026-05-16  same line
"""
    new_text, _arc, _report = consolidate_text(text)
    assert "A line of prose" in new_text
    assert "maintainer note" in new_text


def test_consolidate_preserves_multiple_sections() -> None:
    from anthill.core.memory_hygiene import consolidate_text
    text = """## Preferences

- 2026-05-01  concise

## Working style

- 2026-05-02  product manager

## Languages

- 2026-05-03  Chinese
"""
    new_text, _arc, report = consolidate_text(text)
    assert "## Preferences" in new_text
    assert "## Working style" in new_text
    assert "## Languages" in new_text
    assert report.changed is False  # no duplicates, no overflow


def test_consolidate_empty_file_is_noop() -> None:
    from anthill.core.memory_hygiene import consolidate_text
    new_text, archive, report = consolidate_text("")
    assert archive == ""
    assert report.changed is False


# --- wrappers (filesystem) -------------------------------------------------


def test_consolidate_user_md_writes_back_and_archive(tmp_path: Path) -> None:
    from anthill.core.memory_files import write_user_md
    from anthill.core.memory_hygiene import (
        MAX_LINES_PER_SECTION,
        USER_ARCHIVE_FILENAME,
        consolidate_user_md,
    )
    lines = "\n".join(
        f"- 2026-05-{i:02d}  unique line {i}"
        for i in range(1, MAX_LINES_PER_SECTION + 4)
    )
    write_user_md(tmp_path, f"## Preferences\n\n{lines}\n")
    report = consolidate_user_md(tmp_path)
    assert report.archived == 3
    assert (tmp_path / USER_ARCHIVE_FILENAME).exists()


def test_consolidate_user_md_noop_for_missing_file(tmp_path: Path) -> None:
    from anthill.core.memory_hygiene import consolidate_user_md
    report = consolidate_user_md(tmp_path)
    assert report.changed is False


def test_archive_appends_across_runs(tmp_path: Path) -> None:
    """Two consolidation passes both leave their overflow in the
    archive — second one extends, doesn't overwrite."""
    from anthill.core.memory_files import write_user_md
    from anthill.core.memory_hygiene import (
        MAX_LINES_PER_SECTION,
        USER_ARCHIVE_FILENAME,
        consolidate_user_md,
    )
    first = "\n".join(
        f"- 2026-04-{i:02d}  first-batch line {i}"
        for i in range(1, MAX_LINES_PER_SECTION + 3)
    )
    write_user_md(tmp_path, f"## Preferences\n\n{first}\n")
    consolidate_user_md(tmp_path)
    archive_path = tmp_path / USER_ARCHIVE_FILENAME
    first_size = archive_path.stat().st_size

    # Second batch.
    new_lines = "\n".join(
        f"- 2026-05-{i:02d}  second-batch line {i}"
        for i in range(1, MAX_LINES_PER_SECTION + 3)
    )
    existing = (tmp_path / "USER.md").read_text()
    write_user_md(tmp_path, existing + "\n" + new_lines + "\n")
    consolidate_user_md(tmp_path)

    # Archive grew, didn't shrink.
    assert archive_path.stat().st_size > first_size
    text = archive_path.read_text()
    assert "first-batch" in text
    assert "second-batch" in text
