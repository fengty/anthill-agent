"""0.1.63 — cross-session text search.

Hermes uses SQLite FTS5 for `/insights --days N` over the message
store. anthill keeps everything as JSONL (human-readable, git-able,
no schema migration), so we ship a grep-style search instead.

Trade-off: O(total bytes) scan per query vs O(log n) FTS lookup.
For a typical user with a few hundred sessions of dozens of turns
each, that's still <100 MB of JSONL — a sub-second grep on any
modern machine. We surface a hard cap to keep this predictable.

Returns ranked hits ordered by recency. Each hit carries enough
context (request snippet, timestamp, session_id) that the REPL
can render a useful one-line summary and let the user drill in
with `/session show <id>`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from anthill.core.sessions import sessions_dir


# Hard cap on files scanned per query. Keeps `/search` responsive
# even when the sessions/ dir grows unbounded over months of use.
# Newest files are scanned first; cap mostly affects deep history.
DEFAULT_FILE_LIMIT = 200

# Per-file max bytes — protects against pathologically large sessions
# (one rare ask with a 200KB final_output) dominating scan time.
DEFAULT_PER_FILE_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class SearchHit:
    """One matching turn in a session."""

    session_id: str
    ts: float
    request: str       # truncated for display
    snippet: str       # text around the match (with the matched text)
    match_field: str   # "request" | "output"


def _snippet(text: str, match_start: int, match_end: int, width: int = 60) -> str:
    """Render `…before match after…` style context around a hit."""
    head = max(0, match_start - width // 2)
    tail = min(len(text), match_end + width // 2)
    prefix = "…" if head > 0 else ""
    suffix = "…" if tail < len(text) else ""
    return f"{prefix}{text[head:tail]}{suffix}"


def _iter_turns_from_file(
    path: Path, *, max_bytes: int
) -> Iterable[dict]:
    """Yield each `kind == 'turn'` record from a session JSONL.

    Stops at `max_bytes` to bound per-file work. Tolerates trailing
    garbage (power-cut writes leave half-records) by skipping any
    line that doesn't parse — same defensive shape as load_session.
    """
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes)
    except OSError:
        return
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict) and record.get("kind") == "turn":
            yield record


def search_sessions(
    query: str,
    *,
    home: Path,
    limit: int = 20,
    file_limit: int = DEFAULT_FILE_LIMIT,
    per_file_max_bytes: int = DEFAULT_PER_FILE_MAX_BYTES,
    ignore_case: bool = True,
) -> list[SearchHit]:
    """Grep over session JSONL. Returns the newest `limit` matches.

    `query` is a plain substring by default (case-insensitive). A
    leading `/` switches to regex mode (`/foo.*bar/`). This mirrors
    common grep conventions without needing a quoting flag.

    Matching scans BOTH the request text AND the final_output. If
    the same turn matches both, we report ONE hit prioritizing the
    request match (more concise snippet).
    """
    if not query.strip():
        return []

    # Detect regex mode: surrounding /…/ or /…
    is_regex = False
    raw = query.strip()
    if raw.startswith("/") and len(raw) >= 2:
        is_regex = True
        # Strip leading / and optional trailing /
        raw = raw[1:]
        if raw.endswith("/"):
            raw = raw[:-1]

    if is_regex:
        try:
            pattern = re.compile(
                raw, re.IGNORECASE if ignore_case else 0
            )
        except re.error:
            # Invalid regex → return empty, don't crash. REPL surfaces
            # the empty result and the user can fix the pattern.
            return []
    else:
        # Plain substring → escape the query so meta chars are literal.
        pattern = re.compile(
            re.escape(raw),
            re.IGNORECASE if ignore_case else 0,
        )

    sdir = sessions_dir(home)
    if not sdir.exists():
        return []

    # Newest files first. mtime is a stable proxy for "most recent
    # session"; a session that was resumed yesterday is more
    # interesting than one finished a month ago.
    files = sorted(
        (p for p in sdir.iterdir() if p.suffix == ".jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:file_limit]

    hits: list[SearchHit] = []
    for path in files:
        session_id = path.stem
        for record in _iter_turns_from_file(
            path, max_bytes=per_file_max_bytes
        ):
            req = str(record.get("request") or "")
            out = str(record.get("final_output") or "")
            ts = float(record.get("ts") or 0.0)

            m_req = pattern.search(req)
            if m_req is not None:
                hits.append(
                    SearchHit(
                        session_id=session_id,
                        ts=ts,
                        request=req[:80],
                        snippet=_snippet(req, m_req.start(), m_req.end()),
                        match_field="request",
                    )
                )
                continue  # don't double-count if output also matches

            m_out = pattern.search(out)
            if m_out is not None:
                hits.append(
                    SearchHit(
                        session_id=session_id,
                        ts=ts,
                        request=req[:80],
                        snippet=_snippet(out, m_out.start(), m_out.end()),
                        match_field="output",
                    )
                )
                if len(hits) >= limit * 3:
                    # Soft pre-cap so we don't accumulate thousands of
                    # hits when the query matches very common tokens.
                    break
        if len(hits) >= limit * 3:
            break

    # Final sort by recency descending, take top `limit`.
    hits.sort(key=lambda h: h.ts, reverse=True)
    return hits[:limit]
