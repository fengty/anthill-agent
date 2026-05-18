"""0.1.31 — cross-session FTS5 recall index.

Bridges the gap between the in-session rolling window (0.1.28) and
the distilled USER.md / MEMORY.md (0.1.29). Indexes every history
entry into a per-nation SQLite FTS5 table so `/recall <query>` and
the future auto-context-fetch step can surface things the user
asked about weeks ago.

Tests cover:
- index lifecycle: ensure / close / row_count
- single-entry index + dedup on re-index
- bulk rebuild from history.jsonl
- FTS5 search ranking (best match first)
- query sanitization (no FTS5 syntax errors from user input)
- output snippet truncation
- best-effort failure modes (corrupt DB, missing FTS5 returns empty)
"""

from __future__ import annotations

import time
from pathlib import Path


def _entry(req: str, output: str = "ok", *, ts: float | None = None, eid: str | None = None):
    """HistoryEntry factory that mirrors what nation.ask actually writes."""
    from anthill.core.history import HistoryEntry

    ts = ts if ts is not None else time.time()
    return HistoryEntry(
        id=eid or HistoryEntry.make_id(req, ts),
        timestamp=ts,
        request=req,
        plan=[{"task_type": "general", "depends_on": []}],
        outcomes=[{"status": "ok", "output": output}],
    )


# --- lifecycle -----------------------------------------------------------


def test_ensure_creates_db_file(tmp_path: Path) -> None:
    from anthill.core.recall import RecallIndex, recall_db_path

    idx = RecallIndex(recall_db_path(tmp_path))
    assert idx.ensure() is True
    assert recall_db_path(tmp_path).exists()
    idx.close()


def test_row_count_empty_on_fresh_db(tmp_path: Path) -> None:
    from anthill.core.recall import RecallIndex, recall_db_path

    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    assert idx.row_count() == 0
    idx.close()


# --- single-entry index --------------------------------------------------


def test_index_entry_then_search_finds_it(tmp_path: Path) -> None:
    from anthill.core.recall import RecallIndex, recall_db_path

    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    idx.index_entry(_entry("translate this paragraph to French"))
    hits = idx.search("translate French")
    assert len(hits) == 1
    assert "translate" in hits[0].request.lower()
    idx.close()


def test_search_ranks_better_match_first(tmp_path: Path) -> None:
    from anthill.core.recall import RecallIndex, recall_db_path

    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    idx.index_entry(_entry("translate this to French and explain choices"))
    idx.index_entry(_entry("research the X protocol"))
    idx.index_entry(_entry("french cuisine is rich in butter"))
    hits = idx.search("translate French")
    # The translate entry should rank above the cuisine match.
    assert hits[0].request.startswith("translate")
    idx.close()


def test_search_includes_output_text(tmp_path: Path) -> None:
    """The synthesised final output is indexed too — recall finds an
    entry by what the answer contained, not just what was asked."""
    from anthill.core.recall import RecallIndex, recall_db_path

    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    idx.index_entry(_entry(
        "translate this",
        output="Bonjour, c'est la traduction française du paragraphe.",
    ))
    hits = idx.search("française")
    assert len(hits) == 1
    idx.close()


def test_reindex_replaces_existing_row(tmp_path: Path) -> None:
    """Indexing the same entry id twice updates the FTS row in place."""
    from anthill.core.history import HistoryEntry
    from anthill.core.recall import RecallIndex, recall_db_path

    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    eid = "abc12345"
    idx.index_entry(HistoryEntry(
        id=eid, timestamp=1.0, request="original text", plan=[], outcomes=[],
    ))
    idx.index_entry(HistoryEntry(
        id=eid, timestamp=1.0, request="updated text", plan=[], outcomes=[],
    ))
    # row_count stays 1, search hits the updated text.
    assert idx.row_count() == 1
    hits = idx.search("updated")
    assert len(hits) == 1
    no_hit = idx.search("original")
    assert no_hit == []
    idx.close()


# --- bulk rebuild from history -----------------------------------------


def test_rebuild_from_history_indexes_everything(tmp_path: Path) -> None:
    from anthill.core.history import append_history
    from anthill.core.recall import RecallIndex, recall_db_path

    # Seed history with three entries.
    for req in ("translate hello to French",
                "research the X protocol",
                "what's the weather"):
        append_history(_entry(req), tmp_path)

    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    n = idx.rebuild_from_history(tmp_path)
    assert n == 3
    assert idx.row_count() == 3
    idx.close()


def test_search_history_helper_auto_rebuilds(tmp_path: Path) -> None:
    """One-shot helper bulk-rebuilds when the index is empty —
    so first /recall after upgrade just works."""
    from anthill.core.history import append_history
    from anthill.core.recall import search_history

    append_history(_entry("research the X protocol"), tmp_path)
    hits = search_history(tmp_path, "protocol")
    assert len(hits) == 1


# --- query sanitization -------------------------------------------------


def test_query_with_fts5_metachars_doesnt_crash(tmp_path: Path) -> None:
    """User queries can contain `:` / `-` / `*` / quotes — none of
    those should crash the FTS5 MATCH parser."""
    from anthill.core.recall import RecallIndex, recall_db_path

    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    idx.index_entry(_entry("translate hello to French"))
    # Pathological queries that would otherwise blow up MATCH.
    for q in ('"unclosed quote', "minus-sign-only", "*", "col:value", "()"):
        hits = idx.search(q)
        assert isinstance(hits, list)  # no crash; may be empty
    idx.close()


def test_empty_query_returns_empty(tmp_path: Path) -> None:
    from anthill.core.recall import RecallIndex, recall_db_path

    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    idx.index_entry(_entry("anything"))
    assert idx.search("") == []
    assert idx.search("   ") == []
    idx.close()


# --- snippet truncation -------------------------------------------------


def test_long_output_gets_truncated_snippet(tmp_path: Path) -> None:
    from anthill.core.recall import RecallIndex, recall_db_path

    long_output = "X " * 5000
    idx = RecallIndex(recall_db_path(tmp_path))
    idx.ensure()
    idx.index_entry(_entry("dummy", output=long_output))
    hits = idx.search("X")
    assert len(hits) == 1
    # Snippet is bounded for display — recall.py caps at 240 chars.
    assert len(hits[0].output_snippet) <= 250
    idx.close()


# --- best-effort failure mode -------------------------------------------


def test_search_returns_empty_when_db_corrupt(tmp_path: Path) -> None:
    """A garbled DB file should yield empty results, not raise."""
    from anthill.core.recall import RecallIndex, recall_db_path

    path = recall_db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a sqlite database")
    idx = RecallIndex(path)
    # ensure may return False; downstream methods stay safe.
    idx.ensure()
    hits = idx.search("anything")
    assert hits == []
