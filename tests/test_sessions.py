"""0.1.35 — sessions as persisted JSONL.

The first patch of the "connective-tissue arc" — see
``docs/experience.md``. Closes the "resume across days" gap by
persisting every REPL turn to ``~/.anthill/sessions/<id>.jsonl``
so `anthill --resume` can pick the thread back up.

Tests cover:
- start_session creates file + start record
- append_turn writes JSON line + updates in-memory list
- load_session round-trips a multi-turn file
- prefix lookup ("sess-abc1" finds "sess-abc12345")
- list_sessions ordering + nation filter
- most_recent_session respects idle policy
- corrupt / truncated files don't crash
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# --- start / append / load round-trip ----------------------------------


def test_start_session_creates_file_and_start_record(tmp_path: Path) -> None:
    from anthill.core.sessions import session_path, start_session

    s = start_session(tmp_path, "default")
    assert s.session_id.startswith("sess-")
    path = session_path(tmp_path, s.session_id)
    assert path.exists()
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first["kind"] == "start"
    assert first["nation"] == "default"


def test_append_turn_writes_jsonl(tmp_path: Path) -> None:
    from anthill.core.sessions import (
        SessionTurn,
        session_path,
        start_session,
    )

    s = start_session(tmp_path, "default")
    s.append_turn(
        SessionTurn(
            ts=1000.0,
            request="hello",
            final_output="hi",
            plan=[{"task_type": "general", "depends_on": []}],
            outcomes_summary=[{"status": "ok", "task_type": "general"}],
            duration_seconds=0.5,
        )
    )
    assert s.turn_count == 1
    lines = session_path(tmp_path, s.session_id).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    turn = json.loads(lines[1])
    assert turn["kind"] == "turn"
    assert turn["request"] == "hello"
    assert turn["duration"] == 0.5


def test_load_session_round_trip(tmp_path: Path) -> None:
    from anthill.core.sessions import (
        SessionTurn,
        load_session,
        start_session,
    )

    s = start_session(tmp_path, "default")
    s.append_turn(SessionTurn(ts=1.0, request="a", final_output="A"))
    s.append_turn(SessionTurn(ts=2.0, request="b", final_output="B"))
    reloaded = load_session(s.session_id, tmp_path)
    assert reloaded is not None
    assert reloaded.session_id == s.session_id
    assert reloaded.turn_count == 2
    assert reloaded.turns[0].request == "a"
    assert reloaded.turns[1].final_output == "B"


def test_load_session_returns_none_when_missing(tmp_path: Path) -> None:
    from anthill.core.sessions import load_session

    assert load_session("sess-nope", tmp_path) is None


def test_prefix_match_resolves_unique_id(tmp_path: Path) -> None:
    """`anthill --resume sess-abc1` finds `sess-abc12345` when unique."""
    from anthill.core.sessions import load_session, start_session

    s = start_session(tmp_path, "default")
    prefix = s.session_id[:10]   # "sess-abc12"
    reloaded = load_session(prefix, tmp_path)
    assert reloaded is not None
    assert reloaded.session_id == s.session_id


def test_prefix_match_ambiguous_returns_none(tmp_path: Path) -> None:
    """When two sessions share a prefix, the lookup is ambiguous."""
    import unittest.mock

    from anthill.core.sessions import load_session, start_session

    # Force the same prefix by monkey-patching uuid.
    fake_uuids = iter([
        type("U", (), {"hex": "abcdefghijklmnop"})(),
        type("U", (), {"hex": "abcdefghqrstuvwx"})(),
    ])
    with unittest.mock.patch("anthill.core.sessions.uuid.uuid4", lambda: next(fake_uuids)):
        start_session(tmp_path, "default")
        start_session(tmp_path, "default")
    # "sess-abcdefgh" matches both; lookup must fail safe.
    assert load_session("sess-abcdefgh", tmp_path) is None


# --- listing + recency policy -----------------------------------------


def test_list_sessions_most_recent_first(tmp_path: Path) -> None:
    from anthill.core.sessions import (
        SessionTurn,
        list_sessions,
        start_session,
    )

    s1 = start_session(tmp_path, "default")
    s1.append_turn(SessionTurn(ts=1000.0, request="old", final_output="x"))
    s2 = start_session(tmp_path, "default")
    s2.append_turn(SessionTurn(ts=2000.0, request="new", final_output="x"))
    metas = list_sessions(tmp_path)
    assert metas[0].session_id == s2.session_id
    assert metas[1].session_id == s1.session_id


def test_list_sessions_filters_by_nation(tmp_path: Path) -> None:
    from anthill.core.sessions import (
        SessionTurn,
        list_sessions,
        start_session,
    )

    s_default = start_session(tmp_path, "default")
    s_default.append_turn(SessionTurn(ts=1.0, request="a", final_output="A"))
    s_other = start_session(tmp_path, "work")
    s_other.append_turn(SessionTurn(ts=2.0, request="b", final_output="B"))
    metas = list_sessions(tmp_path, nation_name="work")
    assert len(metas) == 1
    assert metas[0].nation_name == "work"


def test_most_recent_session_respects_idle_window(tmp_path: Path) -> None:
    from anthill.core.sessions import (
        SessionTurn,
        most_recent_session,
        start_session,
    )

    s = start_session(tmp_path, "default")
    s.append_turn(SessionTurn(
        ts=time.time() - 3600,  # 1h ago — well within 24h
        request="warm",
        final_output="ok",
    ))
    warm = most_recent_session(tmp_path, "default")
    assert warm is not None
    assert warm.session_id == s.session_id


def test_most_recent_session_none_when_stale(tmp_path: Path) -> None:
    """48h-old session shouldn't auto-resume."""
    from anthill.core.sessions import (
        SessionTurn,
        most_recent_session,
        start_session,
    )

    s = start_session(tmp_path, "default")
    s.append_turn(SessionTurn(
        ts=time.time() - 86400 * 2,
        request="cold",
        final_output="ok",
    ))
    assert most_recent_session(tmp_path, "default") is None


def test_most_recent_session_only_matching_nation(tmp_path: Path) -> None:
    from anthill.core.sessions import (
        SessionTurn,
        most_recent_session,
        start_session,
    )

    s = start_session(tmp_path, "work")
    s.append_turn(SessionTurn(ts=time.time(), request="a", final_output="A"))
    # asking for 'default' shouldn't return the 'work' session.
    assert most_recent_session(tmp_path, "default") is None


def test_list_sessions_empty_when_no_dir(tmp_path: Path) -> None:
    from anthill.core.sessions import list_sessions

    fresh = tmp_path / "nope"
    assert list_sessions(fresh) == []


# --- corruption tolerance --------------------------------------------


def test_truncated_line_tolerated(tmp_path: Path) -> None:
    """A power-cut write leaves a half-line at the end; load should ignore it."""
    from anthill.core.sessions import load_session, session_path, start_session

    s = start_session(tmp_path, "default")
    # Append a deliberate bad line to the file.
    with session_path(tmp_path, s.session_id).open("a") as f:
        f.write('{"kind": "turn", "ts": 1.0, "request": "good", "final_output": "ok"}\n')
        f.write('{"kind": "turn", "ts": ')  # truncated JSON
    reloaded = load_session(s.session_id, tmp_path)
    assert reloaded is not None
    assert reloaded.turn_count == 1
    assert reloaded.turns[0].request == "good"


def test_end_session_appends_marker(tmp_path: Path) -> None:
    from anthill.core.sessions import end_session, session_path, start_session

    s = start_session(tmp_path, "default")
    end_session(s, reason="user_quit")
    lines = session_path(tmp_path, s.session_id).read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    assert last["kind"] == "end"
    assert last["reason"] == "user_quit"


def test_end_session_noop_when_file_missing(tmp_path: Path) -> None:
    """Calling end_session on a missing path doesn't raise."""
    from anthill.core.sessions import Session, end_session

    s = Session(
        session_id="sess-fake",
        nation_name="default",
        started_at=0.0,
        path=tmp_path / "nope.jsonl",
    )
    # Should not raise.
    end_session(s, reason="x")


# --- SessionTurn ↔ dict ---------------------------------------------


def test_session_turn_dict_round_trip() -> None:
    from anthill.core.sessions import SessionTurn

    t = SessionTurn(
        ts=42.0,
        request="r",
        final_output="o",
        plan=[{"task_type": "x", "depends_on": []}],
        outcomes_summary=[{"status": "ok", "task_type": "x"}],
        duration_seconds=1.5,
    )
    d = t.to_dict()
    assert d["kind"] == "turn"
    t2 = SessionTurn.from_dict(d)
    assert t2.request == "r"
    assert t2.duration_seconds == 1.5
    assert t2.plan == [{"task_type": "x", "depends_on": []}]


def test_session_turn_from_dict_tolerates_missing_fields() -> None:
    from anthill.core.sessions import SessionTurn

    t = SessionTurn.from_dict({"kind": "turn"})
    assert t.request == ""
    assert t.final_output == ""
    assert t.plan == []


@pytest.fixture(autouse=False)
def _no_real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))
