"""Background job tests — spawn, list, status detection, cancel.

The orchestration is mostly subprocess management. We avoid invoking
the real `anthill ask` (which would need an API key and minutes) by
using a fake binary that runs a tiny shell sleep + echo. The status
machinery is the same either way.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from anthill.core.background import (
    BackgroundJob,
    bg_dir,
    cancel_job,
    clear_job,
    list_jobs,
    load_job,
    read_log,
    start_background,
)


# --- helpers ---------------------------------------------------------------


def _make_fake_anthill_bin(tmp_path: Path, script_body: str) -> Path:
    """Write a tiny shell script that masquerades as `anthill` for the test.

    The bg module calls `<bin> ask <request> --nation <n>`; the script
    we generate ignores the args and runs whatever body the test asks
    for. Marked executable so /bin/sh can invoke it.
    """
    bin_path = tmp_path / "fake-anthill"
    bin_path.write_text("#!/bin/sh\n" + script_body + "\n")
    bin_path.chmod(0o755)
    return bin_path


def _wait_for_completion(job_id: str, nat_dir: Path, *, timeout: float = 5.0) -> BackgroundJob:
    """Poll until done.json appears or timeout. Fail-loud on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = load_job(job_id, nat_dir)
        if job is None:
            time.sleep(0.05)
            continue
        if job.exit_code is not None:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# --- Spawn + metadata -----------------------------------------------------


def test_start_background_writes_meta_and_returns_job(tmp_path: Path) -> None:
    """The parent should see a job back immediately, before the child finishes."""
    bin_path = _make_fake_anthill_bin(tmp_path, "echo 'hello from bg'; exit 0")
    job = start_background(
        "test request",
        "default",
        tmp_path,
        anthill_bin=str(bin_path),
    )

    assert job.job_id
    assert job.pid > 0
    assert job.request == "test request"
    assert (job.job_dir / "meta.json").exists()
    meta = json.loads((job.job_dir / "meta.json").read_text())
    assert meta["request"] == "test request"
    assert meta["nation"] == "default"
    assert meta["pid"] == job.pid


def test_completed_job_status_is_completed(tmp_path: Path) -> None:
    bin_path = _make_fake_anthill_bin(tmp_path, "echo 'done'; exit 0")
    job = start_background("any", "default", tmp_path, anthill_bin=str(bin_path))
    final = _wait_for_completion(job.job_id, tmp_path)
    assert final.status == "completed"
    assert final.exit_code == 0
    assert "done" in read_log(final)


def test_failed_job_status_is_failed(tmp_path: Path) -> None:
    """Non-zero exit ⇒ failed (distinct from 'died')."""
    bin_path = _make_fake_anthill_bin(tmp_path, "echo 'oops' >&2; exit 7")
    job = start_background("any", "default", tmp_path, anthill_bin=str(bin_path))
    final = _wait_for_completion(job.job_id, tmp_path)
    assert final.status == "failed"
    assert final.exit_code == 7
    assert "oops" in read_log(final)


# --- Discovery ------------------------------------------------------------


def test_list_jobs_newest_first(tmp_path: Path) -> None:
    bin_path = _make_fake_anthill_bin(tmp_path, "exit 0")
    j1 = start_background("first", "default", tmp_path, anthill_bin=str(bin_path))
    _wait_for_completion(j1.job_id, tmp_path)
    time.sleep(0.05)  # ensure distinct started_at
    j2 = start_background("second", "default", tmp_path, anthill_bin=str(bin_path))
    _wait_for_completion(j2.job_id, tmp_path)

    listing = list_jobs(tmp_path)
    assert len(listing) == 2
    assert listing[0].job_id == j2.job_id  # newer first
    assert listing[1].job_id == j1.job_id


def test_load_job_supports_prefix_match(tmp_path: Path) -> None:
    bin_path = _make_fake_anthill_bin(tmp_path, "exit 0")
    job = start_background("any", "default", tmp_path, anthill_bin=str(bin_path))
    _wait_for_completion(job.job_id, tmp_path)

    short = job.job_id[:3]
    loaded = load_job(short, tmp_path)
    assert loaded is not None
    assert loaded.job_id == job.job_id


def test_load_job_returns_none_for_missing(tmp_path: Path) -> None:
    assert load_job("nonexistent", tmp_path) is None


def test_list_jobs_on_empty_nation(tmp_path: Path) -> None:
    assert list_jobs(tmp_path) == []


# --- Cancel + clear --------------------------------------------------------


def test_cancel_running_job(tmp_path: Path) -> None:
    """A long-running job should be killable; status becomes 'cancelled'."""
    bin_path = _make_fake_anthill_bin(tmp_path, "sleep 30; exit 0")
    job = start_background("long", "default", tmp_path, anthill_bin=str(bin_path))

    # Give the OS a beat to actually start the process.
    time.sleep(0.1)
    assert cancel_job(job.job_id, tmp_path) is True

    # Wait for the process to actually die. We don't strictly need
    # done.json — the cancelled marker is what flips the status.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        j = load_job(job.job_id, tmp_path)
        assert j is not None
        if j.status == "cancelled":
            return
        time.sleep(0.05)
    raise AssertionError("cancellation did not take effect within 5s")


def test_cancel_finished_job_returns_false(tmp_path: Path) -> None:
    bin_path = _make_fake_anthill_bin(tmp_path, "exit 0")
    job = start_background("any", "default", tmp_path, anthill_bin=str(bin_path))
    _wait_for_completion(job.job_id, tmp_path)
    # Already gone — cancel should report nothing happened.
    assert cancel_job(job.job_id, tmp_path) is False


def test_clear_finished_job_removes_directory(tmp_path: Path) -> None:
    bin_path = _make_fake_anthill_bin(tmp_path, "exit 0")
    job = start_background("any", "default", tmp_path, anthill_bin=str(bin_path))
    _wait_for_completion(job.job_id, tmp_path)
    assert job.job_dir.exists()

    assert clear_job(job.job_id, tmp_path) is True
    assert not job.job_dir.exists()


def test_clear_running_job_refuses(tmp_path: Path) -> None:
    """Don't let `clear` accidentally orphan a live process."""
    bin_path = _make_fake_anthill_bin(tmp_path, "sleep 5; exit 0")
    job = start_background("long", "default", tmp_path, anthill_bin=str(bin_path))
    time.sleep(0.1)
    try:
        assert clear_job(job.job_id, tmp_path) is False
        assert job.job_dir.exists()
    finally:
        cancel_job(job.job_id, tmp_path)


# --- Log handling ---------------------------------------------------------


def test_read_log_truncates_large_output(tmp_path: Path) -> None:
    """Don't blow up the terminal on a 10MB log."""
    bin_path = _make_fake_anthill_bin(
        tmp_path,
        # Print ~50KB of output so we can test truncation cleanly.
        "for i in $(seq 1 5000); do echo 'XXXXXXXXX'; done; exit 0",
    )
    job = start_background("verbose", "default", tmp_path, anthill_bin=str(bin_path))
    final = _wait_for_completion(job.job_id, tmp_path)

    truncated = read_log(final, max_bytes=1000)
    assert "truncated" in truncated
    assert len(truncated.encode("utf-8")) <= 1200  # 1000 bytes + small prefix


def test_corrupt_meta_is_skipped(tmp_path: Path) -> None:
    """A garbled meta.json should not crash `list`."""
    bg = bg_dir(tmp_path)
    bg.mkdir(parents=True)
    bad = bg / "deadbeef"
    bad.mkdir()
    (bad / "meta.json").write_text("not valid json")
    assert list_jobs(tmp_path) == []
