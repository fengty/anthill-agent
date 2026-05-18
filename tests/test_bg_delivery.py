"""0.1.37 — background → delivery routing.

Closes the "Background doesn't deliver" ❌ in experience.md §4.
Before: `start_background` spawned a child that wrote done.json
on finish; nobody told the user. After: meta.json carries
origin_surface + origin_session_id; mark_delivered + pending_deliveries
let the REPL surface completions to the originating session at
the next prompt.

Tests focus on the data layer (origin tagging, delivery dedup,
filter by surface/session). REPL prompt-loop integration is
covered indirectly: the notifier just calls pending_deliveries +
mark_delivered, both tested below.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def _fake_job(
    nation_dir: Path,
    *,
    job_id: str = "abc12345",
    origin_surface: str = "repl",
    origin_session_id: str = "sess-xyz",
    request: str = "do the thing",
    done: bool = True,
    exit_code: int = 0,
) -> Path:
    """Write a synthetic job dir like start_background would."""
    jd = nation_dir / "bg" / job_id
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "meta.json").write_text(json.dumps({
        "job_id": job_id,
        "request": request,
        "nation": "default",
        "pid": 99999,
        "started_at": time.time() - 60,
        "origin_surface": origin_surface,
        "origin_session_id": origin_session_id,
    }))
    if done:
        (jd / "done.json").write_text(json.dumps({
            "exit_code": exit_code,
            "completed_at": time.time(),
        }))
    (jd / "output.log").write_text("final answer here\n")
    return jd


# --- origin tagging ----------------------------------------------------


def test_load_job_reads_origin_fields(tmp_path: Path) -> None:
    from anthill.core.background import load_job

    _fake_job(tmp_path, origin_surface="repl", origin_session_id="sess-abc")
    job = load_job("abc12345", tmp_path)
    assert job is not None
    assert job.origin_surface == "repl"
    assert job.origin_session_id == "sess-abc"


def test_load_job_old_meta_without_origin_defaults_unknown(tmp_path: Path) -> None:
    """A meta.json from before 0.1.37 has no origin fields; should
    load fine with origin_surface='unknown'."""
    from anthill.core.background import load_job

    jd = tmp_path / "bg" / "oldjob01"
    jd.mkdir(parents=True)
    (jd / "meta.json").write_text(json.dumps({
        "job_id": "oldjob01",
        "request": "x",
        "nation": "default",
        "pid": 0,
        "started_at": time.time(),
    }))
    (jd / "done.json").write_text('{"exit_code": 0, "completed_at": 1}')
    job = load_job("oldjob01", tmp_path)
    assert job is not None
    assert job.origin_surface == "unknown"
    assert job.origin_session_id == ""


# --- delivery dedup ----------------------------------------------------


def test_mark_delivered_creates_marker(tmp_path: Path) -> None:
    from anthill.core.background import load_job, mark_delivered

    _fake_job(tmp_path)
    job = load_job("abc12345", tmp_path)
    assert job is not None
    assert job.delivered_at is None
    mark_delivered(job)
    assert (job.job_dir / "delivered.json").exists()
    assert job.delivered_at is not None


def test_load_after_mark_returns_delivered_at(tmp_path: Path) -> None:
    from anthill.core.background import load_job, mark_delivered

    _fake_job(tmp_path)
    job = load_job("abc12345", tmp_path)
    assert job is not None
    mark_delivered(job)
    again = load_job("abc12345", tmp_path)
    assert again is not None
    assert again.delivered_at is not None


# --- pending_deliveries filter logic -----------------------------------


def test_pending_lists_only_completed_undelivered(tmp_path: Path) -> None:
    from anthill.core.background import pending_deliveries

    _fake_job(tmp_path, job_id="job00001")             # completed
    _fake_job(tmp_path, job_id="job00002", done=False)  # still running
    pending = pending_deliveries(tmp_path)
    assert [j.job_id for j in pending] == ["job00001"]


def test_pending_filters_by_origin_surface(tmp_path: Path) -> None:
    from anthill.core.background import pending_deliveries

    _fake_job(tmp_path, job_id="job00001", origin_surface="repl")
    _fake_job(tmp_path, job_id="job00002", origin_surface="cli")
    _fake_job(tmp_path, job_id="job00003", origin_surface="im")
    repl_only = pending_deliveries(tmp_path, origin_surface="repl")
    assert [j.job_id for j in repl_only] == ["job00001"]


def test_pending_filters_by_session_id(tmp_path: Path) -> None:
    from anthill.core.background import pending_deliveries

    _fake_job(
        tmp_path, job_id="job00001",
        origin_surface="repl", origin_session_id="sess-aaa",
    )
    _fake_job(
        tmp_path, job_id="job00002",
        origin_surface="repl", origin_session_id="sess-bbb",
    )
    only_aaa = pending_deliveries(
        tmp_path, origin_surface="repl", origin_session_id="sess-aaa",
    )
    assert [j.job_id for j in only_aaa] == ["job00001"]


def test_pending_skips_delivered(tmp_path: Path) -> None:
    from anthill.core.background import (
        mark_delivered,
        pending_deliveries,
    )

    _fake_job(tmp_path)
    pending = pending_deliveries(tmp_path)
    assert len(pending) == 1
    mark_delivered(pending[0])
    pending_again = pending_deliveries(tmp_path)
    assert pending_again == []


def test_pending_includes_failed_jobs(tmp_path: Path) -> None:
    """A failed bg job should ALSO get a notification — user wants
    to know it crashed, not just successes."""
    from anthill.core.background import pending_deliveries

    _fake_job(tmp_path, exit_code=1)
    pending = pending_deliveries(tmp_path)
    assert len(pending) == 1
    assert pending[0].status == "failed"


def test_pending_empty_when_no_bg_dir(tmp_path: Path) -> None:
    """Fresh nation with no bg jobs ever started — empty list, no
    crash even though `bg/` doesn't exist."""
    from anthill.core.background import pending_deliveries

    fresh = tmp_path / "no-bg-here"
    fresh.mkdir()
    assert pending_deliveries(fresh) == []


# --- start_background origin tagging -----------------------------------


def test_start_background_writes_origin_fields(tmp_path: Path, monkeypatch) -> None:
    """The actual spawn would fork a child; we mock subprocess.Popen
    to keep the test fast and POSIX-portable."""
    import subprocess

    from anthill.core.background import start_background

    class _FakeProc:
        pid = 12345

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _FakeProc())
    job = start_background(
        "demo", "default", tmp_path,
        origin_surface="repl",
        origin_session_id="sess-test",
    )
    assert job.origin_surface == "repl"
    assert job.origin_session_id == "sess-test"
    meta = json.loads((job.job_dir / "meta.json").read_text())
    assert meta["origin_surface"] == "repl"
    assert meta["origin_session_id"] == "sess-test"
