"""0.1.63 — `/search` cross-session grep tests."""

from __future__ import annotations

import json
from pathlib import Path

from anthill.core.session_search import SearchHit, search_sessions


def _write_session(home: Path, sid: str, turns: list[dict]) -> Path:
    """Lay down a session JSONL with start record + turns + end."""
    sdir = home / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{sid}.jsonl"
    lines: list[str] = [
        json.dumps({"kind": "start", "session_id": sid, "ts": 0.0})
    ]
    for t in turns:
        lines.append(json.dumps({"kind": "turn", **t}))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    _write_session(tmp_path, "sess-1", [{"ts": 1.0, "request": "hi", "final_output": "hello"}])
    assert search_sessions("", home=tmp_path) == []
    assert search_sessions("   ", home=tmp_path) == []


def test_search_no_sessions_dir_returns_empty(tmp_path: Path) -> None:
    assert search_sessions("anything", home=tmp_path) == []


def test_search_matches_request_text(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "sess-1",
        [{"ts": 1.0, "request": "analyze zentao bug 12345", "final_output": "done"}],
    )
    hits = search_sessions("zentao", home=tmp_path)
    assert len(hits) == 1
    assert hits[0].match_field == "request"
    assert "zentao" in hits[0].snippet.lower()


def test_search_matches_output_when_no_request_hit(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "sess-1",
        [
            {
                "ts": 1.0,
                "request": "what is the capital of france",
                "final_output": "Paris is the capital. ...",
            }
        ],
    )
    hits = search_sessions("Paris", home=tmp_path)
    assert len(hits) == 1
    assert hits[0].match_field == "output"


def test_search_case_insensitive_by_default(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "sess-1",
        [{"ts": 1.0, "request": "ZenTao Bug Analysis", "final_output": ""}],
    )
    # Lowercase query hits mixed-case content.
    hits = search_sessions("zentao", home=tmp_path)
    assert len(hits) == 1


def test_search_regex_mode_via_slash_wrapper(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "sess-1",
        [
            {"ts": 1.0, "request": "bug 12345", "final_output": ""},
            {"ts": 2.0, "request": "task 67890", "final_output": ""},
        ],
    )
    # /bug\s+\d+/ matches "bug 12345" but NOT "task 67890".
    hits = search_sessions("/bug\\s+\\d+/", home=tmp_path)
    assert len(hits) == 1
    assert "bug 12345" in hits[0].snippet


def test_search_regex_invalid_returns_empty(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "sess-1",
        [{"ts": 1.0, "request": "test", "final_output": ""}],
    )
    # Unclosed group → invalid regex → empty (don't crash).
    assert search_sessions("/[unclosed/", home=tmp_path) == []


def test_search_orders_by_recency_descending(tmp_path: Path) -> None:
    _write_session(
        tmp_path, "sess-old",
        [{"ts": 100.0, "request": "first zentao question", "final_output": ""}],
    )
    _write_session(
        tmp_path, "sess-new",
        [{"ts": 200.0, "request": "later zentao followup", "final_output": ""}],
    )
    hits = search_sessions("zentao", home=tmp_path)
    assert len(hits) == 2
    assert hits[0].ts == 200.0
    assert hits[1].ts == 100.0


def test_search_limit_caps_results(tmp_path: Path) -> None:
    turns = [
        {"ts": float(i), "request": f"zentao {i}", "final_output": ""}
        for i in range(50)
    ]
    _write_session(tmp_path, "sess-many", turns)
    hits = search_sessions("zentao", home=tmp_path, limit=5)
    assert len(hits) == 5
    # Newest 5 ts: 49, 48, 47, 46, 45
    assert [h.ts for h in hits] == [49.0, 48.0, 47.0, 46.0, 45.0]


def test_search_dedupes_per_turn_on_request_match(tmp_path: Path) -> None:
    """If the same turn matches in both request AND output, only one
    hit fires (we don't double-count)."""
    _write_session(
        tmp_path,
        "sess-1",
        [{
            "ts": 1.0,
            "request": "zentao bug",
            "final_output": "Indeed, zentao bug analysis here.",
        }],
    )
    hits = search_sessions("zentao", home=tmp_path)
    assert len(hits) == 1
    # Request match wins (more concise snippet).
    assert hits[0].match_field == "request"


def test_search_tolerates_garbage_lines(tmp_path: Path) -> None:
    """Power-cut writes leave partial lines. Garbage must be skipped,
    not crash the search."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    path = sdir / "sess-corrupt.jsonl"
    path.write_text(
        json.dumps({"kind": "start", "session_id": "s", "ts": 0}) + "\n"
        + json.dumps({"kind": "turn", "ts": 1, "request": "zentao bug", "final_output": ""}) + "\n"
        + "this is not json at all\n"
        + '{"kind": "turn", "ts": 2, "request": "another zentao", "final'
    )
    hits = search_sessions("zentao", home=tmp_path)
    assert len(hits) == 1  # one well-formed turn matched


def test_search_returns_searchhit_dataclass(tmp_path: Path) -> None:
    _write_session(
        tmp_path, "sess-1",
        [{"ts": 1.0, "request": "match this", "final_output": ""}],
    )
    hits = search_sessions("match", home=tmp_path)
    assert isinstance(hits[0], SearchHit)
    assert hits[0].session_id == "sess-1"
    assert hits[0].request.startswith("match this")


def test_search_snippet_truncation_for_long_text(tmp_path: Path) -> None:
    long_output = "a" * 500 + " ZENTAO bug here " + "b" * 500
    _write_session(
        tmp_path, "sess-1",
        [{"ts": 1.0, "request": "x", "final_output": long_output}],
    )
    hits = search_sessions("zentao", home=tmp_path)
    assert len(hits) == 1
    # Snippet shouldn't be 1000+ chars; should contain the match.
    assert len(hits[0].snippet) < 200
    assert "zentao" in hits[0].snippet.lower()


def test_search_skips_pre_044_records_without_request(tmp_path: Path) -> None:
    """Records that lack request/output fields don't blow up the
    grep — they just produce no matches."""
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    (sdir / "sess-1.jsonl").write_text(
        json.dumps({"kind": "turn", "ts": 1.0}) + "\n"  # malformed turn
    )
    assert search_sessions("anything", home=tmp_path) == []
