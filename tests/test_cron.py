"""0.1.67 — cron scheduler tests.

Anthill stays daemon-less: `anthill cron tick` runs all due jobs.
These tests cover the pure logic (schedule parsing, due detection,
store I/O) — the CLI integration is thin wiring.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from anthill.core.cron import (
    JobSpec,
    add_job,
    due_jobs,
    load_jobs,
    next_due_at,
    remove_job,
    save_jobs,
    validate_schedule,
)


# --- schedule grammar ---------------------------------------------------


def test_validate_known_schedules() -> None:
    for ok in [
        "@hourly",
        "@daily 09:00",
        "@daily 23:59",
        "@every 30s",
        "@every 5m",
        "@every 2h",
        "@every 1d",
    ]:
        assert validate_schedule(ok) is None, f"{ok!r} should validate"


def test_validate_rejects_unknown() -> None:
    for bad in [
        "",
        "later",
        "@daily",            # missing time
        "@daily 25:00",      # bad hour
        "@daily 09:60",      # bad minute
        "@every",            # missing N+unit
        "@every 10",         # missing unit
        "@every 0m",         # zero interval
        "@every -5m",        # negative
        "cron(* * * * *)",   # not in our grammar (yet)
    ]:
        assert validate_schedule(bad) is not None, f"{bad!r} should be invalid"


def test_hourly_next_due_aligns_to_top_of_hour() -> None:
    # 2026-05-19 10:23:45 → next hourly = 2026-05-19 11:00:00
    base = datetime(2026, 5, 19, 10, 23, 45).timestamp()
    nd = next_due_at("@hourly", after=base, created_at=base)
    assert nd is not None
    nd_dt = datetime.fromtimestamp(nd)
    assert nd_dt.hour == 11
    assert nd_dt.minute == 0
    assert nd_dt.second == 0


def test_daily_next_due_today_when_time_not_yet_passed() -> None:
    # At 08:00, "@daily 09:00" fires today.
    base = datetime(2026, 5, 19, 8, 0, 0).timestamp()
    nd = next_due_at("@daily 09:00", after=base, created_at=base)
    assert nd is not None
    nd_dt = datetime.fromtimestamp(nd)
    assert nd_dt.year == 2026 and nd_dt.month == 5 and nd_dt.day == 19
    assert nd_dt.hour == 9 and nd_dt.minute == 0


def test_daily_next_due_tomorrow_when_time_already_passed() -> None:
    # At 10:00, "@daily 09:00" → next day.
    base = datetime(2026, 5, 19, 10, 0, 0).timestamp()
    nd = next_due_at("@daily 09:00", after=base, created_at=base)
    assert nd is not None
    nd_dt = datetime.fromtimestamp(nd)
    assert nd_dt.day == 20
    assert nd_dt.hour == 9 and nd_dt.minute == 0


def test_every_n_units() -> None:
    base = 1_000_000.0
    assert next_due_at("@every 30s", after=base, created_at=base) == base + 30
    assert next_due_at("@every 5m", after=base, created_at=base) == base + 300
    assert next_due_at("@every 2h", after=base, created_at=base) == base + 7200
    assert next_due_at("@every 1d", after=base, created_at=base) == base + 86400


def test_next_due_unknown_returns_none() -> None:
    assert next_due_at("nonsense", after=time.time(), created_at=time.time()) is None


# --- JobSpec serialization ----------------------------------------------


def test_jobspec_round_trip() -> None:
    j = JobSpec(
        schedule="@daily 09:00",
        request="summarize standups",
        channel_name="slack",
        channel_target="C123",
        toolset_allow=["web_fetch", "file_read"],
        nation="default",
    )
    rt = JobSpec.from_dict(j.to_dict())
    assert rt.schedule == j.schedule
    assert rt.request == j.request
    assert rt.channel_name == j.channel_name
    assert rt.channel_target == j.channel_target
    assert rt.toolset_allow == j.toolset_allow


def test_jobspec_from_dict_tolerates_missing_fields() -> None:
    """Hand-edited jobs.json with sparse fields shouldn't crash load."""
    j = JobSpec.from_dict({"schedule": "@hourly", "request": "ping"})
    assert j.id  # auto-generated
    assert j.enabled is True
    assert j.last_run_at is None
    assert j.toolset_allow == []


# --- store I/O ----------------------------------------------------------


def test_save_and_load_empty_store(tmp_path: Path) -> None:
    assert load_jobs(tmp_path) == []
    save_jobs([], tmp_path)
    assert load_jobs(tmp_path) == []


def test_add_and_load_jobs(tmp_path: Path) -> None:
    add_job(tmp_path, JobSpec(schedule="@hourly", request="ping"))
    add_job(tmp_path, JobSpec(schedule="@daily 09:00", request="report"))
    jobs = load_jobs(tmp_path)
    assert len(jobs) == 2
    assert {j.request for j in jobs} == {"ping", "report"}


def test_remove_job_by_full_id(tmp_path: Path) -> None:
    add_job(tmp_path, JobSpec(id="abc12345", schedule="@hourly", request="a"))
    add_job(tmp_path, JobSpec(id="def67890", schedule="@hourly", request="b"))
    assert remove_job(tmp_path, "abc12345") is True
    remaining = load_jobs(tmp_path)
    assert [j.id for j in remaining] == ["def67890"]


def test_remove_job_by_prefix(tmp_path: Path) -> None:
    add_job(tmp_path, JobSpec(id="abc12345", schedule="@hourly", request="a"))
    add_job(tmp_path, JobSpec(id="def67890", schedule="@hourly", request="b"))
    assert remove_job(tmp_path, "abc") is True
    assert [j.id for j in load_jobs(tmp_path)] == ["def67890"]


def test_remove_job_ambiguous_prefix_fails(tmp_path: Path) -> None:
    add_job(tmp_path, JobSpec(id="abc12345", schedule="@hourly", request="a"))
    add_job(tmp_path, JobSpec(id="abc67890", schedule="@hourly", request="b"))
    assert remove_job(tmp_path, "abc") is False
    # Neither was removed.
    assert len(load_jobs(tmp_path)) == 2


def test_load_tolerates_garbage_json(tmp_path: Path) -> None:
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text("this is not json")
    assert load_jobs(tmp_path) == []


# --- due detection ------------------------------------------------------


def test_due_jobs_empty_when_no_jobs() -> None:
    assert due_jobs([]) == []


def test_due_jobs_returns_jobs_past_next_due() -> None:
    now = time.time()
    # Created an hour ago, fires hourly → due now.
    j_due = JobSpec(
        schedule="@hourly",
        request="ping",
        created_at=now - 3700,
        last_run_at=None,
    )
    # Created 5 minutes ago, fires every hour → NOT due.
    j_not_due = JobSpec(
        schedule="@every 1h",
        request="other",
        created_at=now - 300,
        last_run_at=None,
    )
    due = due_jobs([j_due, j_not_due], now=now)
    assert j_due in due
    assert j_not_due not in due


def test_due_jobs_skips_disabled() -> None:
    now = time.time()
    j = JobSpec(
        schedule="@every 1s",
        request="x",
        created_at=now - 100,
        last_run_at=None,
        enabled=False,
    )
    assert due_jobs([j], now=now) == []


def test_due_jobs_uses_last_run_at_as_anchor() -> None:
    """A job that JUST ran shouldn't fire again immediately."""
    now = time.time()
    j = JobSpec(
        schedule="@every 1h",
        request="x",
        created_at=now - 7200,
        last_run_at=now - 60,  # ran 1 minute ago
    )
    assert due_jobs([j], now=now) == []


def test_due_jobs_invalid_schedule_skipped() -> None:
    """A hand-edited job with a bad schedule shouldn't crash the
    tick — just don't fire it."""
    now = time.time()
    j = JobSpec(schedule="bogus", request="x", created_at=now - 1000)
    assert due_jobs([j], now=now) == []
