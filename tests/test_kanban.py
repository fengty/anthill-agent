"""0.2.31 — Kanban task board.

Tests cover the storage layer (SQLite CRUD) and the agent-loop
tool wrappers. Real-scenario flows are exercised via the integrated
agent_loop in test_kanban_integration.
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
    delete_task,
    kanban_path,
    list_comments,
    list_tasks,
    show_task,
    update_status,
)
from anthill.core.kanban_tools import make_kanban_executors
from anthill.core.tools_protocol import ToolCall


# --- storage round-trips ----------------------------------------------


def test_create_and_show_roundtrip(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="check ports", body="lsof -i :8080")
    assert tid > 0
    task = show_task(tmp_path, tid)
    assert task is not None
    assert task.title == "check ports"
    assert task.body == "lsof -i :8080"
    assert task.status == "pending"


def test_missing_id_returns_none(tmp_path: Path) -> None:
    assert show_task(tmp_path, 9999) is None


def test_list_hides_completed_by_default(tmp_path: Path) -> None:
    a = create_task(tmp_path, title="active task")
    b = create_task(tmp_path, title="finished task")
    update_status(tmp_path, b, "completed", summary="done")

    active = list_tasks(tmp_path)
    titles = {t.title for t in active}
    assert "active task" in titles
    assert "finished task" not in titles


def test_list_can_show_completed(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="done")
    update_status(tmp_path, tid, "completed", summary="ok")
    tasks = list_tasks(tmp_path, status="completed")
    assert len(tasks) == 1
    assert tasks[0].status == "completed"


def test_update_status_stamps_completed_at(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="x")
    before = time.time()
    update_status(tmp_path, tid, "completed", summary="done")
    task = show_task(tmp_path, tid)
    assert task.completed_at is not None
    assert task.completed_at >= before


def test_rejects_invalid_status(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="x")
    with pytest.raises(ValueError):
        update_status(tmp_path, tid, "made-up-status")


def test_create_rejects_empty_title(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_task(tmp_path, title="   ")


def test_metadata_serializes(tmp_path: Path) -> None:
    """JSON metadata round-trips through the DB."""
    tid = create_task(
        tmp_path,
        title="x",
        metadata={"changed_files": ["a.py", "b.py"], "test_count": 12},
    )
    task = show_task(tmp_path, tid)
    assert task.metadata["test_count"] == 12
    assert task.metadata["changed_files"] == ["a.py", "b.py"]


# --- comments ---------------------------------------------------------


def test_comments_round_trip(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="x")
    add_comment(tmp_path, tid, "first thought", author="ant-1")
    add_comment(tmp_path, tid, "follow-up", author="user")
    comments = list_comments(tmp_path, tid)
    assert len(comments) == 2
    assert comments[0].author == "ant-1"
    assert comments[0].text == "first thought"


def test_comment_touches_task_updated_at(tmp_path: Path) -> None:
    """A new comment should bump updated_at so the task surfaces on
    the active board."""
    tid = create_task(tmp_path, title="x")
    task_a = show_task(tmp_path, tid)
    time.sleep(0.01)
    add_comment(tmp_path, tid, "ping")
    task_b = show_task(tmp_path, tid)
    assert task_b.updated_at > task_a.updated_at


# --- atomic claim -----------------------------------------------------


def test_claim_next_picks_oldest_pending(tmp_path: Path) -> None:
    """Tasks are claimed in created_at order, oldest first."""
    a = create_task(tmp_path, title="first")
    time.sleep(0.005)
    b = create_task(tmp_path, title="second")
    claimed = claim_next(tmp_path, "ant-1")
    assert claimed is not None
    assert claimed.id == a
    # Status flipped to in_progress.
    assert claimed.status == "in_progress"
    assert claimed.assignee == "ant-1"


def test_claim_next_returns_none_when_board_empty(tmp_path: Path) -> None:
    assert claim_next(tmp_path, "ant-1") is None


def test_claim_skips_already_assigned(tmp_path: Path) -> None:
    """If the oldest task is already claimed, the next claim picks
    the NEXT oldest pending."""
    a = create_task(tmp_path, title="taken")
    b = create_task(tmp_path, title="available")
    claim_next(tmp_path, "ant-1")  # takes a
    claimed = claim_next(tmp_path, "ant-2")  # should take b
    assert claimed.id == b
    assert claimed.assignee == "ant-2"


# --- summary ---------------------------------------------------------


def test_board_summary_counts(tmp_path: Path) -> None:
    a = create_task(tmp_path, title="p1")
    b = create_task(tmp_path, title="p2")
    c = create_task(tmp_path, title="done")
    update_status(tmp_path, c, "completed", summary="ok")
    summary = board_summary(tmp_path)
    assert summary["pending"] == 2
    assert summary["completed"] == 1


# --- agent loop tool wrappers ----------------------------------------


def test_kanban_show_tool_returns_active_board(tmp_path: Path) -> None:
    create_task(tmp_path, title="task A")
    create_task(tmp_path, title="task B")
    dispatch, _ = make_kanban_executors(tmp_path)
    result = asyncio.run(dispatch(
        ToolCall(id="t1", name="kanban_show", arguments={})
    ))
    assert not result.is_error
    assert "task A" in result.content
    assert "task B" in result.content


def test_kanban_show_tool_with_id(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="specific task", body="details here")
    add_comment(tmp_path, tid, "a comment")
    dispatch, _ = make_kanban_executors(tmp_path)
    result = asyncio.run(dispatch(
        ToolCall(id="t1", name="kanban_show", arguments={"id": tid})
    ))
    assert not result.is_error
    assert "specific task" in result.content
    assert "details here" in result.content
    assert "a comment" in result.content


def test_kanban_create_tool(tmp_path: Path) -> None:
    dispatch, _ = make_kanban_executors(tmp_path, default_assignee="ant-X")
    result = asyncio.run(dispatch(
        ToolCall(
            id="t1", name="kanban_create",
            arguments={"title": "new task", "body": "do the thing"},
        )
    ))
    assert not result.is_error
    assert "created task #" in result.content
    # Task IS in the board with the assignee set.
    tasks = list_tasks(tmp_path)
    assert tasks[0].title == "new task"
    assert tasks[0].assignee == "ant-X"


def test_kanban_complete_tool(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="x")
    dispatch, _ = make_kanban_executors(tmp_path)
    result = asyncio.run(dispatch(
        ToolCall(
            id="t1", name="kanban_complete",
            arguments={"id": tid, "summary": "all done"},
        )
    ))
    assert not result.is_error
    task = show_task(tmp_path, tid)
    assert task.status == "completed"
    assert task.summary == "all done"


def test_kanban_block_tool(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="needs creds")
    dispatch, _ = make_kanban_executors(tmp_path)
    result = asyncio.run(dispatch(
        ToolCall(
            id="t1", name="kanban_block",
            arguments={"id": tid, "reason": "need API token"},
        )
    ))
    assert not result.is_error
    task = show_task(tmp_path, tid)
    assert task.status == "blocked"


def test_kanban_comment_tool(tmp_path: Path) -> None:
    tid = create_task(tmp_path, title="x")
    dispatch, _ = make_kanban_executors(tmp_path, default_assignee="ant-Y")
    result = asyncio.run(dispatch(
        ToolCall(
            id="t1", name="kanban_comment",
            arguments={"id": tid, "text": "checked the logs"},
        )
    ))
    assert not result.is_error
    comments = list_comments(tmp_path, tid)
    assert len(comments) == 1
    assert comments[0].text == "checked the logs"
    assert comments[0].author == "ant-Y"


def test_kanban_claim_tool(tmp_path: Path) -> None:
    create_task(tmp_path, title="claimable")
    dispatch, _ = make_kanban_executors(tmp_path)
    result = asyncio.run(dispatch(
        ToolCall(
            id="t1", name="kanban_claim",
            arguments={"assignee": "ant-Z"},
        )
    ))
    assert not result.is_error
    assert "claimed task" in result.content


def test_kanban_claim_when_empty(tmp_path: Path) -> None:
    """Empty board → ok response (not an error), just 'no pending'."""
    dispatch, _ = make_kanban_executors(tmp_path)
    result = asyncio.run(dispatch(
        ToolCall(
            id="t1", name="kanban_claim",
            arguments={"assignee": "ant-Z"},
        )
    ))
    assert not result.is_error
    assert "no pending tasks" in result.content


def test_kanban_unknown_tool(tmp_path: Path) -> None:
    """Unknown name routes to a structured error."""
    dispatch, _ = make_kanban_executors(tmp_path)
    result = asyncio.run(dispatch(
        ToolCall(id="t1", name="kanban_teleport", arguments={})
    ))
    assert result.is_error


def test_kanban_path_under_home(tmp_path: Path) -> None:
    """The DB lands in <home>/kanban.db."""
    p = kanban_path(tmp_path)
    assert p.parent == tmp_path
    assert p.name == "kanban.db"


# --- dispatch_with_kanban (end-to-end through dispatch_tool_call) ----


def test_dispatch_with_kanban_routes_correctly(tmp_path: Path) -> None:
    """The compound dispatch (bash + browser + kanban) routes by tool name."""
    from anthill.core.tool_executors import make_dispatch_with_kanban
    dispatch = make_dispatch_with_kanban(tmp_path, default_assignee="ant-1")

    # bash_run still works.
    bash_result = asyncio.run(dispatch(
        ToolCall(id="t1", name="bash_run", arguments={"cmd": "echo hi"})
    ))
    assert "hi" in bash_result.content

    # kanban tools work too.
    create_result = asyncio.run(dispatch(
        ToolCall(
            id="t2", name="kanban_create",
            arguments={"title": "via dispatch"},
        )
    ))
    assert not create_result.is_error
    tasks = list_tasks(tmp_path)
    assert tasks[0].title == "via dispatch"
