"""0.2.31 — Kanban SQLite task board.

Trimmed (0.2.43) from 25 to 10 tests. Storage CRUD + tool wrappers
+ compound dispatch.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from anthill.core.kanban import (
    add_comment,
    board_summary,
    claim_next,
    create_task,
    list_comments,
    list_tasks,
    show_task,
    update_status,
)
from anthill.core.kanban_tools import make_kanban_executors
from anthill.core.tools_protocol import ToolCall


# --- storage layer ----------------------------------------------------


def test_create_show_update_status_round_trip(tmp_path: Path) -> None:
    """End-to-end: create → show → mark completed → completed_at set
    + status updated + summary stored."""
    tid = create_task(tmp_path, title="check ports", body="lsof -i :8080")
    assert tid > 0
    before = time.time()
    update_status(tmp_path, tid, "completed", summary="done")
    task = show_task(tmp_path, tid)
    assert task.title == "check ports"
    assert task.status == "completed"
    assert task.summary == "done"
    assert task.completed_at is not None and task.completed_at >= before


def test_storage_validates_inputs(tmp_path: Path) -> None:
    """Two guards in one test:
      - empty title → ValueError (no silent garbage tasks)
      - unknown status → ValueError (status enum is bounded)"""
    with pytest.raises(ValueError):
        create_task(tmp_path, title="   ")
    tid = create_task(tmp_path, title="ok")
    with pytest.raises(ValueError):
        update_status(tmp_path, tid, "made-up-status")


def test_list_hides_completed_by_default(tmp_path: Path) -> None:
    """list_tasks(default) shows only active; status="completed"
    surfaces the rest. UI default avoids "stale board with 1000
    completed tasks" noise."""
    a = create_task(tmp_path, title="active")
    b = create_task(tmp_path, title="done")
    update_status(tmp_path, b, "completed", summary="x")
    active_titles = {t.title for t in list_tasks(tmp_path)}
    assert "active" in active_titles and "done" not in active_titles
    completed_titles = {t.title for t in list_tasks(tmp_path, status="completed")}
    assert "done" in completed_titles


def test_comments_roundtrip_and_bump_updated_at(tmp_path: Path) -> None:
    """Comments save + reload, AND each new comment bumps the task's
    updated_at so the task re-surfaces on the active board."""
    tid = create_task(tmp_path, title="x")
    before = show_task(tmp_path, tid).updated_at
    time.sleep(0.01)
    add_comment(tmp_path, tid, "first", author="ant-1")
    add_comment(tmp_path, tid, "second", author="user")
    comments = list_comments(tmp_path, tid)
    assert [c.text for c in comments] == ["first", "second"]
    # Touch contract.
    assert show_task(tmp_path, tid).updated_at > before


def test_claim_next_is_atomic_oldest_first(tmp_path: Path) -> None:
    """Two pending tasks created in order. First claim takes oldest;
    second claim takes the next (no double-claim). Empty board → None."""
    a = create_task(tmp_path, title="first")
    time.sleep(0.01)
    b = create_task(tmp_path, title="second")
    claimed_a = claim_next(tmp_path, "ant-1")
    claimed_b = claim_next(tmp_path, "ant-2")
    assert claimed_a.id == a and claimed_b.id == b
    assert claimed_a.assignee == "ant-1"
    # Both gone.
    assert claim_next(tmp_path, "ant-3") is None


# --- agent-loop tool wrappers ---------------------------------------


def test_kanban_show_tool_listing_and_by_id(tmp_path: Path) -> None:
    """Two surfaces in one test:
      - kanban_show() with no id → renders the active board
      - kanban_show(id=N) → renders one task with its comments"""
    tid = create_task(tmp_path, title="single", body="details")
    add_comment(tmp_path, tid, "a comment")
    dispatch, _ = make_kanban_executors(tmp_path)

    # No id.
    r1 = asyncio.run(dispatch(ToolCall(
        id="t1", name="kanban_show", arguments={})
    ))
    assert "single" in r1.content

    # By id.
    r2 = asyncio.run(dispatch(ToolCall(
        id="t2", name="kanban_show", arguments={"id": tid},
    )))
    assert "details" in r2.content
    assert "a comment" in r2.content


def test_kanban_create_tool_with_assignee_attribution(tmp_path: Path) -> None:
    """kanban_create with default_assignee set → the new task has
    that citizen's id as the assignee."""
    dispatch, _ = make_kanban_executors(tmp_path, default_assignee="ant-X")
    result = asyncio.run(dispatch(ToolCall(
        id="t1", name="kanban_create",
        arguments={"title": "new task", "body": "do it"},
    )))
    assert not result.is_error
    assert list_tasks(tmp_path)[0].assignee == "ant-X"


def test_kanban_complete_and_block_tools(tmp_path: Path) -> None:
    """Both lifecycle endpoints: complete with summary, block with
    reason. Status reflects on the underlying task."""
    tid = create_task(tmp_path, title="x")
    dispatch, _ = make_kanban_executors(tmp_path)

    asyncio.run(dispatch(ToolCall(
        id="t1", name="kanban_complete",
        arguments={"id": tid, "summary": "all done"},
    )))
    assert show_task(tmp_path, tid).status == "completed"

    tid2 = create_task(tmp_path, title="needs review")
    asyncio.run(dispatch(ToolCall(
        id="t2", name="kanban_block",
        arguments={"id": tid2, "reason": "need credentials"},
    )))
    assert show_task(tmp_path, tid2).status == "blocked"


def test_kanban_claim_and_unknown_tool_errors(tmp_path: Path) -> None:
    """kanban_claim returns the claimed task on success and 'no
    pending' on empty board; unknown tool name → structured error."""
    create_task(tmp_path, title="claimable")
    dispatch, _ = make_kanban_executors(tmp_path)

    # Claim hits a real task.
    r1 = asyncio.run(dispatch(ToolCall(
        id="t1", name="kanban_claim",
        arguments={"assignee": "ant-Z"},
    )))
    assert "claimed task" in r1.content

    # Empty board → friendly message, not error.
    r2 = asyncio.run(dispatch(ToolCall(
        id="t2", name="kanban_claim",
        arguments={"assignee": "ant-Z"},
    )))
    assert "no pending" in r2.content

    # Unknown tool name.
    r3 = asyncio.run(dispatch(ToolCall(
        id="t3", name="kanban_teleport", arguments={},
    )))
    assert r3.is_error


# --- compound dispatch + board_summary ------------------------------


def test_dispatch_with_kanban_routes_bash_and_kanban(tmp_path: Path) -> None:
    """make_dispatch_with_kanban routes bash_run AND kanban_*
    correctly — the citizen sees one unified executor."""
    from anthill.core.tool_executors import make_dispatch_with_kanban
    dispatch = make_dispatch_with_kanban(tmp_path, default_assignee="ant-1")

    bash_r = asyncio.run(dispatch(ToolCall(
        id="b1", name="bash_run", arguments={"cmd": "echo hi"},
    )))
    assert "hi" in bash_r.content

    create_r = asyncio.run(dispatch(ToolCall(
        id="k1", name="kanban_create",
        arguments={"title": "via dispatch"},
    )))
    assert not create_r.is_error
    assert list_tasks(tmp_path)[0].title == "via dispatch"
