"""0.1.35 — sessions as persisted JSONL.

The first patch of the "connective-tissue arc" (see
``docs/experience.md`` §6). Closes the "resume across days" gap:
today Anthill's `ConversationContext` (0.1.28) lives in Python
memory and dies with the REPL. `/recall` (0.1.31) finds old asks
but doesn't bring the thread back as the active conversation.

This module gives every REPL session a stable id, persists every
turn to disk as JSONL, and exposes a picker for resume. Mirrors
Claude Code's ``~/.claude/projects/<...>.jsonl`` pattern and
Hermes's per-chat session store with idle reset policies.

Shape of a session file (``~/.anthill/sessions/sess-<id>.jsonl``):

    {"kind": "start", "ts": ..., "session_id": "sess-abc123",
     "nation": "default", "anthill_version": "0.1.35"}
    {"kind": "turn",  "ts": ..., "request": "...", "plan": [...],
     "outcomes": [...], "final_output": "...", "duration": 2.4}
    {"kind": "turn",  "ts": ..., "request": "...", ...}
    {"kind": "end",   "ts": ..., "reason": "graceful"}   # optional

Append-only by design. Power failure mid-write loses at most one
line; the picker / loader tolerate trailing garbage.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SESSIONS_DIR = "sessions"

# 24 hours of idle = the next `anthill` launch starts a fresh session
# by default. Mirrors Hermes's default 1440-minute policy. User can
# override with `anthill --resume <id>` explicitly.
DEFAULT_IDLE_RESET_SECONDS = 24 * 3600


@dataclass
class SessionTurn:
    """One completed exchange in a session.

    Stored as JSON; fields kept narrow on purpose so the file stays
    diff-friendly and the loader is robust across version drift.
    """

    ts: float
    request: str
    final_output: str
    plan: list[dict] = field(default_factory=list)        # [{task_type, depends_on}]
    outcomes_summary: list[dict] = field(default_factory=list)  # [{status, task_type}]
    duration_seconds: float = 0.0
    # 0.1.44 — per-phase wall-clock breakdown so post-hoc analysis can
    # tell whether an outlier ask was Scout-bound or subtask-bound.
    # Optional / default empty so older logs still load.
    timings: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "kind": "turn",
            "ts": self.ts,
            "request": self.request,
            "final_output": self.final_output,
            "plan": self.plan,
            "outcomes_summary": self.outcomes_summary,
            "duration": self.duration_seconds,
        }
        if self.timings:
            d["timings"] = self.timings
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SessionTurn":
        return cls(
            ts=float(data.get("ts") or 0.0),
            request=str(data.get("request") or ""),
            final_output=str(data.get("final_output") or ""),
            plan=list(data.get("plan") or []),
            outcomes_summary=list(data.get("outcomes_summary") or []),
            duration_seconds=float(data.get("duration") or 0.0),
            timings=dict(data.get("timings") or {}),
        )


@dataclass
class Session:
    """A persisted REPL session.

    Has an id, the nation it was opened against, a start timestamp,
    and the list of turns recorded so far. Always tied to a file
    path; modifications append to that file.
    """

    session_id: str
    nation_name: str
    started_at: float
    path: Path
    turns: list[SessionTurn] = field(default_factory=list)

    @property
    def last_turn_at(self) -> float:
        """Timestamp of the most recent turn, or started_at if none."""
        if self.turns:
            return self.turns[-1].ts
        return self.started_at

    @property
    def first_request(self) -> str:
        return self.turns[0].request if self.turns else ""

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def append_turn(self, turn: SessionTurn) -> None:
        """Add a turn to memory AND append it to the JSONL file."""
        self.turns.append(turn)
        _append_line(self.path, turn.to_dict())


@dataclass(frozen=True)
class SessionMeta:
    """Cheap header info for the picker — avoids loading the whole file."""

    session_id: str
    nation_name: str
    started_at: float
    last_turn_at: float
    turn_count: int
    first_request: str
    path: Path


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------


def sessions_dir(home: Path) -> Path:
    """Where all per-session JSONL files live. Created on demand."""
    return home / SESSIONS_DIR


def session_path(home: Path, session_id: str) -> Path:
    return sessions_dir(home) / f"{session_id}.jsonl"


def _new_session_id() -> str:
    return f"sess-{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Start / open / close
# ---------------------------------------------------------------------------


def start_session(home: Path, nation_name: str, *, version: str = "") -> Session:
    """Create a new session file with a fresh id and write the start record."""
    sessions_dir(home).mkdir(parents=True, exist_ok=True)
    sid = _new_session_id()
    path = session_path(home, sid)
    now = time.time()
    start_record = {
        "kind": "start",
        "ts": now,
        "session_id": sid,
        "nation": nation_name,
    }
    if version:
        start_record["anthill_version"] = version
    _append_line(path, start_record)
    return Session(
        session_id=sid,
        nation_name=nation_name,
        started_at=now,
        path=path,
    )


def load_session(session_id: str, home: Path) -> Optional[Session]:
    """Rehydrate a session from its JSONL file. None when missing.

    Accepts a prefix (``sess-abc1...`` ⇒ finds it) for ergonomics — the
    picker shows the short hex, the user can type just enough to be
    unique.
    """
    path = session_path(home, session_id)
    if not path.exists():
        # Prefix lookup.
        dir_path = sessions_dir(home)
        if not dir_path.exists():
            return None
        matches = [
            p for p in dir_path.iterdir()
            if p.suffix == ".jsonl" and p.stem.startswith(session_id)
        ]
        if len(matches) != 1:
            return None
        path = matches[0]
        session_id = path.stem

    session: Session | None = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Tolerate trailing garbage from a power-cut write.
                    continue
                kind = record.get("kind")
                if kind == "start":
                    session = Session(
                        session_id=str(record.get("session_id") or session_id),
                        nation_name=str(record.get("nation") or "default"),
                        started_at=float(record.get("ts") or 0.0),
                        path=path,
                    )
                elif kind == "turn" and session is not None:
                    session.turns.append(SessionTurn.from_dict(record))
                # "end" record is informational; we don't track it
                # beyond knowing the session had a graceful close.
    except OSError:
        return None
    return session


def end_session(session: Session, reason: str = "graceful") -> None:
    """Optional graceful-close record. Safe to omit (no-op if file missing)."""
    if not session.path.exists():
        return
    _append_line(
        session.path,
        {"kind": "end", "ts": time.time(), "reason": reason},
    )


# ---------------------------------------------------------------------------
# Listing / picker support
# ---------------------------------------------------------------------------


def list_sessions(
    home: Path,
    *,
    limit: int = 10,
    nation_name: str | None = None,
) -> list[SessionMeta]:
    """Most-recent first. Optional filter by nation.

    Cheap: walks each file once but only reads the first few records
    + scans line count, so a year of history stays sub-second.
    """
    dir_path = sessions_dir(home)
    if not dir_path.exists():
        return []
    metas: list[SessionMeta] = []
    for path in dir_path.iterdir():
        if path.suffix != ".jsonl":
            continue
        meta = _peek_meta(path)
        if meta is None:
            continue
        if nation_name is not None and meta.nation_name != nation_name:
            continue
        metas.append(meta)
    metas.sort(key=lambda m: m.last_turn_at, reverse=True)
    return metas[:limit]


def most_recent_session(
    home: Path,
    nation_name: str,
    *,
    within_seconds: float = DEFAULT_IDLE_RESET_SECONDS,
) -> Session | None:
    """The most-recent session for this nation, if its last turn is
    within the idle window. Returns None if no recent session OR if
    all candidates are stale — caller starts fresh in that case.

    Used by `anthill` (no `--resume` / `--new-session` flag) to default
    to "continue if still warm, else start fresh."
    """
    metas = list_sessions(home, limit=5, nation_name=nation_name)
    if not metas:
        return None
    now = time.time()
    for meta in metas:
        if now - meta.last_turn_at <= within_seconds:
            return load_session(meta.session_id, home)
    return None


def _peek_meta(path: Path) -> SessionMeta | None:
    """Read just enough of a session file to populate a SessionMeta.

    The `start` record gives us session_id + nation + started_at. The
    FIRST `turn` gives the picker preview text. Total turn count and
    last_turn_at come from a final pass that only reads timestamps —
    avoids parsing 1000-turn answer bodies for a listing.
    """
    session_id = path.stem
    nation_name = "default"
    started_at = 0.0
    first_request = ""
    last_turn_at = 0.0
    turn_count = 0
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = record.get("kind")
                if kind == "start":
                    nation_name = str(record.get("nation") or "default")
                    started_at = float(record.get("ts") or 0.0)
                elif kind == "turn":
                    turn_count += 1
                    if not first_request:
                        first_request = str(record.get("request") or "")
                    last_turn_at = float(record.get("ts") or last_turn_at)
    except OSError:
        return None
    if started_at == 0.0 and last_turn_at == 0.0:
        return None
    return SessionMeta(
        session_id=session_id,
        nation_name=nation_name,
        started_at=started_at,
        last_turn_at=last_turn_at or started_at,
        turn_count=turn_count,
        first_request=first_request,
        path=path,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _append_line(path: Path, record: dict) -> None:
    """Append one JSON line. Best-effort: log + ignore on OSError so
    session persistence never breaks the REPL."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # `with open(..., "a")` is atomic at the line level on POSIX
        # for writes that fit in PIPE_BUF, which our records always do
        # (small JSON dicts). For larger workflow tools the inflight
        # checkpoint pattern remains the right place — sessions are
        # just lightweight turn-by-turn audit trail.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
