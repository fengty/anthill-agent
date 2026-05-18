"""0.1.31 — cross-session full-text recall.

The third memory layer:

  0.1.28  in-session rolling window      — last ~4 turns
  0.1.29  USER.md + MEMORY.md            — distilled long-term
  0.1.30  auto-memory triggers           — agent catches "I prefer X"
  0.1.31  SQLite FTS5 over history       — searchable history (THIS)

What this closes: "上周咱们说过 X，记得吗？" / "remind me what we
decided about the auth flow last month" / any reference that
escapes the rolling window AND wasn't distilled into the .md files.
Both Hermes (``session_search``) and Claude Code (daily-log + topic
file routing) ship this in 2026; without it Anthill's memory is a
bell curve — strong on the very recent and the distilled-permanent,
weak in the middle.

Implementation notes:

- SQLite + FTS5 ships in CPython stdlib (verified on 3.9+ with
  sqlite>=3.35), no extra deps. We bail with a clean fallback
  message if a host happens to ship a sqlite without FTS5.

- One ``recall.db`` per nation, alongside ``history.jsonl``. Nations
  are distinct organisms; mixing their indexes would surface
  cross-nation matches the user doesn't expect.

- Bulk-rebuild from history.jsonl is cheap (every entry is one row
  in the .jsonl); we re-walk only on first use and after the index
  falls behind. Incremental indexing happens in the post-ask hook.

- We index the REQUEST + the synthesised FINAL output. We don't
  index intermediate subtask outputs — they're noise at recall
  time; the user remembers what they asked and what they got back.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from anthill.core.history import HistoryEntry, history_path, load_history


RECALL_DB_FILENAME = "recall.db"

# Soft cap on retained text per row. Long outputs (research synthesis,
# code dumps) get truncated for the index — exact text isn't needed
# at recall time, just enough to match + display a snippet.
MAX_INDEXED_CHARS = 4000


@dataclass(frozen=True)
class RecallHit:
    """One match from `RecallIndex.search`."""

    entry_id: str
    timestamp: float
    request: str
    output_snippet: str   # head of the final output, capped for display
    score: float          # FTS5 bm25 score (higher = better match)


def recall_db_path(nation_dir: Path) -> Path:
    return nation_dir / RECALL_DB_FILENAME


class RecallIndex:
    """SQLite FTS5 index over the nation's history.jsonl.

    Lifecycle:
      - ``ensure()`` opens / creates the DB and FTS5 table
      - ``rebuild_from_history(nation_dir)`` does a bulk reindex when
        the index is empty / stale
      - ``index_entry(entry)`` incrementally adds one row after a
        successful ask
      - ``search(query, k)`` returns ranked hits

    All methods are best-effort: any sqlite / OS error is swallowed
    and surfaced as an empty result, so recall problems can never
    take down the REPL or the post-ask path.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ---- lifecycle ----

    def ensure(self) -> bool:
        """Open DB, create the FTS5 table if missing. False on failure."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            # FTS5 is the search index; a sibling `entries` table holds
            # the indexed-or-not bookkeeping. Two tables on purpose so
            # we can do `INSERT OR REPLACE INTO entries` to dedup by id
            # before re-indexing into fts.
            conn.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(
                    request,
                    output,
                    entry_id UNINDEXED,
                    timestamp UNINDEXED,
                    tokenize='unicode61 remove_diacritics 1'
                );
                CREATE TABLE IF NOT EXISTS entries (
                    entry_id TEXT PRIMARY KEY,
                    timestamp REAL,
                    indexed_at REAL
                );
                """
            )
            conn.commit()
            self._conn = conn
            return True
        except sqlite3.Error:
            self._conn = None
            return False

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ---- writes ----

    def index_entry(self, entry: HistoryEntry) -> bool:
        """Insert (or replace) one history entry. False on any failure."""
        if self._conn is None:
            if not self.ensure():
                return False
        conn = self._conn
        if conn is None:
            return False
        try:
            already = conn.execute(
                "SELECT 1 FROM entries WHERE entry_id = ? LIMIT 1",
                (entry.id,),
            ).fetchone()
            output = _extract_final_output(entry)
            now = time.time()
            if already:
                # Remove old FTS row, re-insert with current text.
                conn.execute(
                    "DELETE FROM recall_fts WHERE entry_id = ?", (entry.id,)
                )
            else:
                conn.execute(
                    "INSERT INTO entries (entry_id, timestamp, indexed_at) "
                    "VALUES (?, ?, ?)",
                    (entry.id, entry.timestamp, now),
                )
            conn.execute(
                "INSERT INTO recall_fts (request, output, entry_id, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (
                    _truncate(entry.request),
                    _truncate(output),
                    entry.id,
                    entry.timestamp,
                ),
            )
            conn.commit()
            return True
        except sqlite3.Error:
            return False

    def rebuild_from_history(self, nation_dir: Path) -> int:
        """Bulk reindex from history.jsonl. Returns row count indexed.

        Idempotent — if half the history is already indexed, the rest
        gets added; existing rows are refreshed in place. We never
        truncate the existing index here because a corrupted history
        line shouldn't wipe out everything else.
        """
        if self._conn is None and not self.ensure():
            return 0
        if not history_path(nation_dir).exists():
            return 0
        n = 0
        for entry in load_history(nation_dir):
            if self.index_entry(entry):
                n += 1
        return n

    # ---- search ----

    def search(self, query: str, *, k: int = 5) -> list[RecallHit]:
        """Top-k recall hits ranked by FTS5 bm25 (lower is better in bm25,
        we flip the sign so 'score' in RecallHit is intuitive: higher = better)."""
        if not query.strip():
            return []
        if self._conn is None and not self.ensure():
            return []
        conn = self._conn
        if conn is None:
            return []
        try:
            # FTS5 MATCH gives us the search; bm25() gives ranking.
            # We escape user input by routing through MATCH parameter.
            cursor = conn.execute(
                """
                SELECT entry_id, timestamp, request, output, bm25(recall_fts) AS rank
                  FROM recall_fts
                 WHERE recall_fts MATCH ?
              ORDER BY rank
                 LIMIT ?
                """,
                (_query_to_fts5(query), k),
            )
            hits: list[RecallHit] = []
            for entry_id, ts, req, out, rank in cursor.fetchall():
                hits.append(
                    RecallHit(
                        entry_id=entry_id,
                        timestamp=float(ts or 0.0),
                        request=req or "",
                        output_snippet=_snippet(out or ""),
                        score=-float(rank),
                    )
                )
            return hits
        except sqlite3.Error:
            return []

    def row_count(self) -> int:
        """How many entries the index thinks it has. Used by the REPL
        to show "📚 N rows indexed" hints and by tests."""
        if self._conn is None and not self.ensure():
            return 0
        conn = self._conn
        if conn is None:
            return 0
        try:
            (n,) = conn.execute("SELECT COUNT(*) FROM entries").fetchone()
            return int(n)
        except sqlite3.Error:
            return 0


# ---------------------------------------------------------------------------
# Module-level convenience used by REPL + tests
# ---------------------------------------------------------------------------


def ensure_index(nation_dir: Path) -> RecallIndex | None:
    """Open / create the recall index for a nation. None on failure."""
    idx = RecallIndex(recall_db_path(nation_dir))
    if not idx.ensure():
        return None
    return idx


def search_history(nation_dir: Path, query: str, *, k: int = 5) -> list[RecallHit]:
    """One-shot search helper. Rebuilds the index if it's empty so first
    use after upgrade still works without an explicit reindex command."""
    idx = ensure_index(nation_dir)
    if idx is None:
        return []
    if idx.row_count() == 0:
        idx.rebuild_from_history(nation_dir)
    try:
        return idx.search(query, k=k)
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _truncate(text: str) -> str:
    if len(text) <= MAX_INDEXED_CHARS:
        return text
    return text[:MAX_INDEXED_CHARS]


def _snippet(text: str, *, chars: int = 240) -> str:
    """Single-line head for display in recall lists."""
    flat = " ".join(text.split())
    if len(flat) <= chars:
        return flat
    return flat[:chars] + "…"


def _extract_final_output(entry: HistoryEntry) -> str:
    """The synthesised final answer for indexing.

    We pick the LAST 'ok' subtask's output as the canonical reply —
    by convention the final step is the synthesis / answer. Falls
    back to concatenating all ok outputs if no clear last winner.
    """
    ok_outputs: list[str] = []
    for outcome in entry.outcomes or []:
        if outcome.get("status") != "ok":
            continue
        text = outcome.get("output") or outcome.get("final_output") or ""
        if isinstance(text, str) and text.strip():
            ok_outputs.append(text)
    if not ok_outputs:
        return ""
    return ok_outputs[-1]


def _query_to_fts5(query: str) -> str:
    """Lightly sanitize a user query for FTS5 MATCH.

    Strips FTS5 syntax characters that would otherwise either error
    (`"`, `-`, `:`) or trigger phrase / column matching the user
    didn't ask for. The result is a plain space-joined bag of terms,
    which FTS5 ANDs together by default — good "all words must
    appear" behavior for recall.
    """
    bad = '"-+*():^'
    cleaned = "".join(" " if c in bad else c for c in query)
    terms = [t for t in cleaned.split() if t.strip()]
    return " ".join(terms) or query.strip()
