"""v0.7.1 — bg jobs link back to history entries.

The closed loop:
  1. `anthill bg ask` spawns subprocess + sets ANTHILL_BG_JOB_ID env var
  2. Subprocess runs nation.ask → build_entry_from_ask reads the env
  3. HistoryEntry stores bg_job_id → persisted in history.jsonl
  4. `anthill bg show <id>` can find the matching history records
  5. `anthill history list` shows a "Src" column distinguishing bg

These tests cover the env-var pipeline + history round-trip + the
lookup helper. The actual subprocess spawn is tested in
test_background.py with a fake binary; here we focus on the data flow.
"""

from __future__ import annotations

from pathlib import Path

from anthill.core.history import (
    HistoryEntry,
    append_history,
    build_entry_from_ask,
    find_by_bg_job,
    load_history,
)


def test_history_entry_dataclass_default_bg_job_id_is_none() -> None:
    """Foreground asks must have bg_job_id=None."""
    e = HistoryEntry(
        id="x", timestamp=1.0, request="r", plan=[], outcomes=[],
    )
    assert e.bg_job_id is None


def test_history_entry_bg_job_id_round_trips() -> None:
    e = HistoryEntry(
        id="x", timestamp=1.0, request="r", plan=[], outcomes=[],
        bg_job_id="deadbeef",
    )
    data = e.to_dict()
    restored = HistoryEntry.from_dict(data)
    assert restored.bg_job_id == "deadbeef"


def test_history_entry_from_dict_missing_bg_field_returns_none() -> None:
    """Pre-v0.7.1 history.jsonl entries have no bg_job_id key — should load clean."""
    e = HistoryEntry.from_dict({
        "id": "x", "timestamp": 1.0, "request": "r",
        "plan": [], "outcomes": [],
    })
    assert e.bg_job_id is None


def test_build_entry_reads_env_var(monkeypatch) -> None:
    """When ANTHILL_BG_JOB_ID is set, the entry inherits it."""
    monkeypatch.setenv("ANTHILL_BG_JOB_ID", "abc12345")
    e = build_entry_from_ask("hello", [], [])
    assert e.bg_job_id == "abc12345"


def test_build_entry_without_env_var_has_no_bg_id(monkeypatch) -> None:
    monkeypatch.delenv("ANTHILL_BG_JOB_ID", raising=False)
    e = build_entry_from_ask("hello", [], [])
    assert e.bg_job_id is None


def test_build_entry_empty_env_var_treated_as_none(monkeypatch) -> None:
    """Empty string env var ⇒ no association (defensive)."""
    monkeypatch.setenv("ANTHILL_BG_JOB_ID", "")
    e = build_entry_from_ask("hello", [], [])
    assert e.bg_job_id is None


def test_find_by_bg_job_returns_only_matching_entries(tmp_path: Path, monkeypatch) -> None:
    # Two entries with bg_job_id, one foreground.
    monkeypatch.setenv("ANTHILL_BG_JOB_ID", "job-A")
    append_history(build_entry_from_ask("first bg ask", [], []), tmp_path)
    append_history(build_entry_from_ask("second bg ask", [], []), tmp_path)
    monkeypatch.delenv("ANTHILL_BG_JOB_ID")
    append_history(build_entry_from_ask("foreground ask", [], []), tmp_path)
    monkeypatch.setenv("ANTHILL_BG_JOB_ID", "job-B")
    append_history(build_entry_from_ask("other bg ask", [], []), tmp_path)

    job_a = find_by_bg_job("job-A", tmp_path)
    assert len(job_a) == 2
    assert all(e.bg_job_id == "job-A" for e in job_a)

    job_b = find_by_bg_job("job-B", tmp_path)
    assert len(job_b) == 1
    assert job_b[0].bg_job_id == "job-B"

    # Unknown id ⇒ empty list, not error.
    assert find_by_bg_job("ghost", tmp_path) == []


def test_find_by_bg_job_ignores_foreground_entries(tmp_path: Path, monkeypatch) -> None:
    """Don't accidentally match foreground entries whose bg_job_id is None."""
    monkeypatch.delenv("ANTHILL_BG_JOB_ID", raising=False)
    append_history(build_entry_from_ask("fg1", [], []), tmp_path)
    append_history(build_entry_from_ask("fg2", [], []), tmp_path)
    assert find_by_bg_job("", tmp_path) == []  # empty string shouldn't match None


def test_bg_job_id_survives_hash_chain(tmp_path: Path, monkeypatch) -> None:
    """The chain hash is computed AFTER the bg_job_id is set, so verify still passes."""
    from anthill.core.history import verify_chain
    monkeypatch.setenv("ANTHILL_BG_JOB_ID", "test-job-id")
    for i in range(3):
        append_history(build_entry_from_ask(f"r{i}", [], []), tmp_path)
    status = verify_chain(tmp_path)
    assert status.ok
    assert status.chained_count == 3
    # And the bg_job_id round-trips through load_history
    entries = load_history(tmp_path)
    assert all(e.bg_job_id == "test-job-id" for e in entries)


def test_start_background_sets_env_var_in_child(tmp_path: Path) -> None:
    """The subprocess fixture: verify the env var is actually set on spawn."""
    from anthill.core.background import (
        load_job,
        start_background,
    )

    # Write a fake anthill script that just dumps the env var and exits.
    bin_path = tmp_path / "fake-anthill"
    bin_path.write_text(
        "#!/bin/sh\n"
        'echo "BG_JOB_ID=$ANTHILL_BG_JOB_ID"\n'
        "exit 0\n"
    )
    bin_path.chmod(0o755)

    job = start_background(
        "some request", "default", tmp_path,
        anthill_bin=str(bin_path),
    )
    # Poll until done.json appears
    import time
    deadline = time.time() + 5.0
    while time.time() < deadline:
        reloaded = load_job(job.job_id, tmp_path)
        if reloaded and reloaded.exit_code is not None:
            break
        time.sleep(0.05)

    log = (job.job_dir / "output.log").read_text()
    assert f"BG_JOB_ID={job.job_id}" in log
