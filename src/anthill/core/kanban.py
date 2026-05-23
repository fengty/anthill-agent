"""0.2.31 — Kanban task board for cross-session work tracking.

Inspired by hermes' kanban: a SQLite-backed shared task board so
that:
  - long-running work survives REPL restart
  - multiple citizens can hand off tasks to each other (0.2.32)
  - the user can see "what is anthill working on" at any moment
  - completed tasks form a durable audit trail

Schema is deliberately small. We're not building Jira — we're
building a stigmergic task surface (citizens see the same board,
react to its state).

Storage location: `~/.anthill/kanban.db` (anthill home, NOT nation
dir). Tasks are global across nations — a /nation switch doesn't
hide the board.

This module is the data layer. The agent-loop tool wrappers live
in `core/kanban_tools.py`; the CLI / REPL surfaces live in
`cli/kanban_commands.py`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# Lifecycle states. Keep this list short — every extra state is
# extra UI to render and extra rule to enforce.
VALID_STATUSES = ("pending", "in_progress", "blocked", "completed", "cancelled")


# --- schema -----------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kanban_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    body        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    assignee    TEXT,                    -- agent_id or NULL (unclaimed)
    parent_id   INTEGER,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    completed_at REAL,
    summary     TEXT,                    -- structured handoff summary
    metadata    TEXT,                    -- JSON blob
    FOREIGN KEY (parent_id) REFERENCES kanban_tasks(id)
);

CREATE TABLE IF NOT EXISTS kanban_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    author      TEXT,                    -- agent_id, "user", or NULL
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES kanban_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON kanban_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON kanban_tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON kanban_tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task ON kanban_comments(task_id);
"""


def kanban_path(home: Path) -> Path:
    """The standard board path under the anthill home dir."""
    return Path(home) / "kanban.db"


def _connect(home: Path) -> sqlite3.Connection:
    """Open the kanban DB, creating it + schema if needed."""
    path = kanban_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # WAL mode so concurrent reads (REPL + background) don't block.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    return conn


# --- dataclasses ------------------------------------------------------


@dataclass
class KanbanTask:
    """One task on the board."""

    id: int
    title: str
    body: str
    status: str
    assignee: Optional[str]
    parent_id: Optional[int]
    created_at: float
    updated_at: float
    completed_at: Optional[float]
    summary: Optional[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "KanbanTask":
        raw_meta = row["metadata"]
        try:
            meta = json.loads(raw_meta) if raw_meta else {}
        except (ValueError, TypeError):
            meta = {}
        return cls(
            id=row["id"],
            title=row["title"],
            body=row["body"] or "",
            status=row["status"],
            assignee=row["assignee"],
            parent_id=row["parent_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            summary=row["summary"],
            metadata=meta,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "status": self.status,
            "assignee": self.assignee,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "summary": self.summary,
            "metadata": self.metadata,
        }


@dataclass
class KanbanComment:
    id: int
    task_id: int
    author: Optional[str]
    text: str
    created_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "KanbanComment":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            author=row["author"],
            text=row["text"],
            created_at=row["created_at"],
        )


# --- CRUD -------------------------------------------------------------


def create_task(
    home: Path,
    *,
    title: str,
    body: str = "",
    assignee: Optional[str] = None,
    parent_id: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """Create a new pending task. Returns the assigned id."""
    if not title or not title.strip():
        raise ValueError("title is required")
    now = time.time()
    meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
    with _connect(home) as conn:
        cur = conn.execute(
            """
            INSERT INTO kanban_tasks
                (title, body, status, assignee, parent_id,
                 created_at, updated_at, metadata)
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                title.strip(),
                body.strip() if body else None,
                assignee,
                parent_id,
                now,
                now,
                meta_json,
            ),
        )
        return int(cur.lastrowid)


def show_task(home: Path, task_id: int) -> Optional[KanbanTask]:
    """Fetch one task by id, or None if missing."""
    with _connect(home) as conn:
        row = conn.execute(
            "SELECT * FROM kanban_tasks WHERE id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return None
    return KanbanTask.from_row(row)


def list_tasks(
    home: Path,
    *,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    limit: int = 50,
    include_completed: bool = False,
) -> list[KanbanTask]:
    """List tasks, most-recently-updated first.

    By default `include_completed=False` hides completed/cancelled
    tasks (those are usually noise on the active board). Pass True
    or `status="completed"` to see them.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        clauses.append("status = ?")
        params.append(status)
    elif not include_completed:
        clauses.append("status NOT IN ('completed', 'cancelled')")
    if assignee is not None:
        clauses.append("assignee = ?")
        params.append(assignee)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = (
        f"SELECT * FROM kanban_tasks{where} "
        f"ORDER BY updated_at DESC LIMIT ?"
    )
    params.append(int(limit))
    with _connect(home) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [KanbanTask.from_row(r) for r in rows]


def update_status(
    home: Path,
    task_id: int,
    new_status: str,
    *,
    summary: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> bool:
    """Change a task's status. Returns True on success.

    `completed` and `cancelled` stamp completed_at automatically.
    `summary` and `metadata` are typically set on completion to
    record handoff data for downstream readers.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"unknown status: {new_status!r}")
    now = time.time()
    completed_at = now if new_status in ("completed", "cancelled") else None
    sets = ["status = ?", "updated_at = ?"]
    params: list[Any] = [new_status, now]
    if completed_at is not None:
        sets.append("completed_at = ?")
        params.append(completed_at)
    if summary is not None:
        sets.append("summary = ?")
        params.append(summary)
    if metadata is not None:
        sets.append("metadata = ?")
        params.append(json.dumps(metadata, ensure_ascii=False))
    sql = f"UPDATE kanban_tasks SET {', '.join(sets)} WHERE id = ?"
    params.append(task_id)
    with _connect(home) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount > 0


def assign_task(home: Path, task_id: int, assignee: Optional[str]) -> bool:
    """Set or clear the assignee. Use None to unassign."""
    now = time.time()
    with _connect(home) as conn:
        cur = conn.execute(
            "UPDATE kanban_tasks SET assignee = ?, updated_at = ? WHERE id = ?",
            (assignee, now, task_id),
        )
        return cur.rowcount > 0


def claim_next(home: Path, assignee: str) -> Optional[KanbanTask]:
    """Atomically claim the oldest unassigned pending task for `assignee`.

    Returns the claimed task or None if nothing is available. The
    update is single-statement so concurrent claimers don't race.
    """
    now = time.time()
    with _connect(home) as conn:
        # Find the candidate. We do find-then-update in a transaction
        # to avoid double-claim under concurrency.
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT id FROM kanban_tasks
                WHERE status = 'pending' AND assignee IS NULL
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            tid = int(row["id"])
            conn.execute(
                """
                UPDATE kanban_tasks
                SET assignee = ?, status = 'in_progress', updated_at = ?
                WHERE id = ?
                """,
                (assignee, now, tid),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        result_row = conn.execute(
            "SELECT * FROM kanban_tasks WHERE id = ?", (tid,)
        ).fetchone()
    return KanbanTask.from_row(result_row) if result_row else None


def add_comment(
    home: Path,
    task_id: int,
    text: str,
    *,
    author: Optional[str] = None,
) -> int:
    """Add a comment to a task. Returns the new comment id."""
    if not text or not text.strip():
        raise ValueError("comment text required")
    now = time.time()
    with _connect(home) as conn:
        cur = conn.execute(
            """
            INSERT INTO kanban_comments (task_id, author, text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, author, text.strip(), now),
        )
        # Touch parent task's updated_at so it surfaces on the board.
        conn.execute(
            "UPDATE kanban_tasks SET updated_at = ? WHERE id = ?",
            (now, task_id),
        )
        return int(cur.lastrowid)


def list_comments(home: Path, task_id: int) -> list[KanbanComment]:
    """All comments on a task in chronological order."""
    with _connect(home) as conn:
        rows = conn.execute(
            """
            SELECT * FROM kanban_comments
            WHERE task_id = ?
            ORDER BY created_at ASC
            """,
            (task_id,),
        ).fetchall()
    return [KanbanComment.from_row(r) for r in rows]


def delete_task(home: Path, task_id: int) -> bool:
    """Hard-delete a task and its comments. Use sparingly — usually
    you want `update_status(..., 'cancelled')` instead so the audit
    trail survives."""
    with _connect(home) as conn:
        conn.execute("DELETE FROM kanban_comments WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM kanban_tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0


# --- summary helpers --------------------------------------------------


def board_summary(home: Path) -> dict[str, int]:
    """Quick "what's on the board" counts. Useful for splash / status."""
    with _connect(home) as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM kanban_tasks
            GROUP BY status
            """
        ).fetchall()
    summary = {s: 0 for s in VALID_STATUSES}
    for r in rows:
        summary[r["status"]] = int(r["n"])
    return summary
