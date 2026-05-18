"""0.1.34 — memory hygiene: keep USER.md / MEMORY.md from rotting.

0.1.29 capped the files at MAX_FILE_CHARS (~8 KB) but the cap was a
dumb tail-truncate. 0.1.30 auto-memory + 0.1.32 user-model inference
write to the files autonomously, so they grow on their own. Without
hygiene the files drift toward:

  - Duplicate entries ("我喜欢简洁回答" appended on five different days)
  - Stale entries (a `## Conventions` line for a project the user
    moved away from six months ago)
  - Just dirty old entries the user wants out of the system prompt

Claude Code solves this with a weekly cron + 200-line cap +
``OLD-MEMORY-ENTRIES.md`` archive. Hermes consolidates when capacity
is exceeded. We adopt the same shape:

  1. **Dedup near-duplicate lines** — set-cosine over tokens, drop
     the older when overlap > 0.85.
  2. **Archive overflow** — when a section exceeds ``MAX_LINES_PER
     _SECTION``, move the oldest entries to ``USER-ARCHIVE.md`` /
     ``MEMORY-ARCHIVE.md`` (one per nation). Archive grows
     unbounded but is never injected into prompts — it's a paper
     trail, not active memory.
  3. **Manual + on-startup** — `/memory consolidate` and
     `/profile consolidate` trigger it explicitly; the REPL also
     runs a tiny check on startup and surfaces "🧹 hygiene needed"
     when the active file is bloated.

This module is pure-stdlib so it ships in the base install and runs
in <50ms on a 5K-line file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from anthill.core.memory_files import (
    MAX_FILE_CHARS,
    _atomic_write,
    nation_memory_path,
    user_md_path,
)


# A section is "bloated" past this many bullet lines. Mirrors Claude
# Code's 200-line design rule, tighter on a section basis since we
# usually have 3-5 sections per file.
MAX_LINES_PER_SECTION = 30

# Token-overlap threshold above which two bullet lines are considered
# the same idea (lower = more aggressive dedup).
DUP_OVERLAP_THRESHOLD = 0.85

# Filename suffixes for the per-file archive (sibling to active file).
USER_ARCHIVE_FILENAME = "USER-ARCHIVE.md"
NATION_ARCHIVE_FILENAME = "MEMORY-ARCHIVE.md"


# Regex to recognize a "bullet line" — `- 2026-05-17  content...`
# Catches the standard format that append_user_md / append_nation_memory
# write, plus the variations users might hand-edit in.
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<body>.+?)\s*$")
_DATE_PREFIX_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<rest>.+)$")


@dataclass(frozen=True)
class Bullet:
    """One ``- YYYY-MM-DD  content`` entry. raw is the literal line."""

    section: str
    date: str        # empty string if no date prefix
    body: str        # the content after the date (or full body if no date)
    raw: str         # the original line as written (for fidelity)


@dataclass
class HygieneReport:
    """What ``consolidate`` actually did. Stable shape for tests + REPL output."""

    deduped: int = 0       # bullet lines removed as near-duplicates
    archived: int = 0      # bullet lines moved to the archive file
    sections_touched: int = 0
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def changed(self) -> bool:
        return self.deduped > 0 or self.archived > 0


# ---------------------------------------------------------------------------
# Section / bullet parsing
# ---------------------------------------------------------------------------


def _parse_sections(text: str) -> "list[tuple[str, list[Bullet], list[str]]]":
    """Walk the markdown, return [(section_name, bullets, other_lines), ...].

    ``other_lines`` holds anything that isn't a parseable bullet (header
    paragraphs, blank lines, comments). We preserve those verbatim so
    consolidation doesn't strip the file's structure.
    """
    sections: list[tuple[str, list[Bullet], list[str]]] = []
    current_section = ""
    current_bullets: list[Bullet] = []
    current_other: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current_section or current_bullets or current_other:
                sections.append((current_section, current_bullets, current_other))
            current_section = line[3:].strip()
            current_bullets = []
            current_other = []
            continue
        match = _BULLET_RE.match(line)
        if match:
            body = match.group("body")
            date = ""
            content = body
            datepart = _DATE_PREFIX_RE.match(body)
            if datepart:
                date = datepart.group("date")
                content = datepart.group("rest").strip()
            current_bullets.append(
                Bullet(
                    section=current_section,
                    date=date,
                    body=content,
                    raw=line,
                )
            )
        else:
            current_other.append(line)
    if current_section or current_bullets or current_other:
        sections.append((current_section, current_bullets, current_other))
    return sections


def _tokens(text: str) -> set[str]:
    """Lowercase token bag. Mirrors core/episodic._tokenize so dedup
    behaves consistently with episodic search."""
    # CJK char-as-token + ASCII word.
    return {
        tok.lower()
        for tok in re.findall(r"[一-鿿]|[a-zA-Z0-9_]+", text)
    }


def _overlap(a: set[str], b: set[str]) -> float:
    """Set-cosine. 1.0 identical, 0.0 disjoint, empty → 0.0."""
    if not a or not b:
        return 0.0
    common = len(a & b)
    return common / ((len(a) * len(b)) ** 0.5)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def needs_hygiene(text: str) -> bool:
    """Cheap pre-check used by the startup hint.

    True when the file is near the file-size cap OR any section has
    too many bullets. Avoids running the full consolidator at every
    REPL launch.
    """
    if len(text) > MAX_FILE_CHARS * 0.75:
        return True
    sections = _parse_sections(text)
    return any(len(bullets) > MAX_LINES_PER_SECTION for _, bullets, _ in sections)


def consolidate_text(text: str) -> "tuple[str, str, HygieneReport]":
    """Run dedup + archive-overflow on a markdown blob.

    Returns ``(new_active_text, appended_archive_text, report)``.
    The active text is what should replace the existing file; the
    archive text is what should be APPENDED to the corresponding
    archive file. Both are empty strings when no changes happen.
    """
    report = HygieneReport(bytes_before=len(text))
    if not text.strip():
        report.bytes_after = report.bytes_before
        return text, "", report

    sections = _parse_sections(text)
    archive_chunks: list[str] = []
    rebuilt_sections: list[tuple[str, list[Bullet], list[str]]] = []

    for section_name, bullets, others in sections:
        if not bullets:
            rebuilt_sections.append((section_name, bullets, others))
            continue

        # --- step 1: dedup. Keep the NEWEST member of each near-dup
        # cluster — newer entries reflect the user's current phrasing.
        bullets_sorted = sorted(
            bullets, key=lambda b: (b.date or ""), reverse=True
        )
        keep: list[Bullet] = []
        keep_tokens: list[set[str]] = []
        deduped_in_section = 0
        for b in bullets_sorted:
            tok = _tokens(b.body)
            if not tok:
                keep.append(b)
                keep_tokens.append(tok)
                continue
            is_dup = False
            for existing_tok in keep_tokens:
                if _overlap(tok, existing_tok) >= DUP_OVERLAP_THRESHOLD:
                    is_dup = True
                    break
            if is_dup:
                deduped_in_section += 1
                continue
            keep.append(b)
            keep_tokens.append(tok)
        report.deduped += deduped_in_section

        # Put back in chronological order (oldest at top) so reading the
        # file in $EDITOR still flows naturally.
        keep.sort(key=lambda b: (b.date or "9999-12-31"))

        # --- step 2: overflow. Anything past MAX_LINES_PER_SECTION
        # (oldest first) goes to the archive.
        if len(keep) > MAX_LINES_PER_SECTION:
            overflow = keep[: len(keep) - MAX_LINES_PER_SECTION]
            keep = keep[len(keep) - MAX_LINES_PER_SECTION :]
            archive_chunk = [f"## {section_name} (archived)"]
            for b in overflow:
                archive_chunk.append(b.raw)
            archive_chunks.append("\n".join(archive_chunk))
            report.archived += len(overflow)

        if deduped_in_section or len(bullets) != len(keep):
            report.sections_touched += 1
        rebuilt_sections.append((section_name, keep, others))

    # --- step 3: render the active file back out.
    out_lines: list[str] = []
    for section_name, bullets, others in rebuilt_sections:
        if section_name:
            out_lines.append(f"## {section_name}")
            out_lines.append("")
        # "others" lives just under the header — keep it positioned there.
        for line in others:
            out_lines.append(line)
        if section_name and others and others[-1].strip():
            out_lines.append("")
        for b in bullets:
            out_lines.append(b.raw)
        out_lines.append("")
    new_text = "\n".join(out_lines).rstrip() + "\n"
    report.bytes_after = len(new_text)

    archive_text = ""
    if archive_chunks:
        archive_text = "\n\n".join(archive_chunks) + "\n"

    return new_text, archive_text, report


def consolidate_user_md(home: Path) -> HygieneReport:
    """Read USER.md, consolidate, write back, append overflow to archive."""
    path = user_md_path(home)
    if not path.exists():
        return HygieneReport()
    text = path.read_text(encoding="utf-8")
    new_text, archive_text, report = consolidate_text(text)
    if report.changed:
        _atomic_write(path, new_text)
        if archive_text:
            archive_path = home / USER_ARCHIVE_FILENAME
            _append_archive(archive_path, archive_text)
    return report


def consolidate_nation_memory(nation_dir: Path) -> HygieneReport:
    """Same as ``consolidate_user_md`` for the per-nation file."""
    path = nation_memory_path(nation_dir)
    if not path.exists():
        return HygieneReport()
    text = path.read_text(encoding="utf-8")
    new_text, archive_text, report = consolidate_text(text)
    if report.changed:
        _atomic_write(path, new_text)
        if archive_text:
            archive_path = nation_dir / NATION_ARCHIVE_FILENAME
            _append_archive(archive_path, archive_text)
    return report


def _append_archive(path: Path, chunk: str) -> None:
    """Append-only writer for the archive files. Bounded only by disk."""
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    new_text = existing.rstrip() + "\n\n" + chunk if existing else chunk
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
