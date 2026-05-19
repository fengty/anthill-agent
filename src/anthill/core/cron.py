"""0.1.67 — scheduled-ask cron.

Hermes ships a full cron scheduler that calls back into the agent at
intervals and supports per-job toolset restrictions + cross-platform
delivery. anthill ships the minimum that actually solves the user
problem: a JSON-backed job store, a simple schedule grammar, and a
`tick` command that runs all due jobs once.

Design:
  - No daemon. We provide `cron tick` that runs all due jobs. Users
    wire it into system cron / launchd / systemd timer to fire it
    every N minutes. This keeps anthill itself stateless between
    ticks (works on serverless / ephemeral containers).
  - Each job optionally targets a channel + recipient (for delivery)
    and an allow-list of toolsets (Hermes-style guard against
    expensive plugins firing on routine summaries).

Schedule grammar (intentionally tiny):

  @hourly                    — top of every hour
  @daily HH:MM               — once a day at HH:MM (local time)
  @every <N><unit>           — N seconds/minutes/hours from creation,
                               then repeating
    unit ∈ {s,m,h,d}

Cron 5-field (`M H DOM MON DOW`) syntax can be added when the simple
grammar isn't enough. For 99% of "daily standup summary at 09:00"
use cases, @daily is plenty.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


def cron_dir(home: Path) -> Path:
    return home / "cron"


def jobs_file(home: Path) -> Path:
    return cron_dir(home) / "jobs.json"


@dataclass
class JobSpec:
    """One scheduled ask."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    schedule: str = "@hourly"
    request: str = ""
    nation: str = "default"
    # Optional delivery target: when set, the completed ask's output
    # is sent to this channel. None = just record to history.
    channel_name: str | None = None
    channel_target: str | None = None  # platform-specific (chat_id / oc_xxx / email addr)
    # Allow-list of toolset names (matches PluginRegistry names). Empty
    # list = no restriction (default registry). Non-empty = ONLY those
    # plugins are exposed for this job's subtasks. Mirrors Hermes's
    # per-job toolset restriction to keep routine summaries cheap.
    toolset_allow: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_run_at: float | None = None
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JobSpec":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            schedule=str(data.get("schedule") or "@hourly"),
            request=str(data.get("request") or ""),
            nation=str(data.get("nation") or "default"),
            channel_name=data.get("channel_name"),
            channel_target=data.get("channel_target"),
            toolset_allow=list(data.get("toolset_allow") or []),
            created_at=float(data.get("created_at") or time.time()),
            last_run_at=(
                float(data["last_run_at"])
                if data.get("last_run_at") is not None
                else None
            ),
            enabled=bool(data.get("enabled", True)),
        )


# --- schedule grammar ---------------------------------------------------


_EVERY_RE = re.compile(r"^@every\s+(\d+)([smhd])$", re.IGNORECASE)
_DAILY_RE = re.compile(r"^@daily\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


def next_due_at(schedule: str, *, after: float, created_at: float) -> float | None:
    """Compute the next firing time strictly AFTER `after`.

    `created_at` is used by @every to anchor the interval. `after`
    is usually `last_run_at or created_at` — i.e. "when did this job
    last fire, or when was it born".

    Returns None when the schedule string can't be parsed. The CLI
    `add` command validates at write time so this should only happen
    on hand-edited jobs.json.
    """
    schedule = schedule.strip()

    # @hourly — next top of hour after `after`.
    if schedule.lower() == "@hourly":
        dt = datetime.fromtimestamp(after)
        next_hour = dt.replace(minute=0, second=0, microsecond=0) + timedelta(
            hours=1
        )
        return next_hour.timestamp()

    # @daily HH:MM — next HH:MM (local) after `after`.
    m = _DAILY_RE.match(schedule)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return None
        dt = datetime.fromtimestamp(after)
        candidate = dt.replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        if candidate.timestamp() <= after:
            candidate = candidate + timedelta(days=1)
        return candidate.timestamp()

    # @every N<unit> — N units after `after` (or `created_at` if no
    # prior run). Unit mapping: s/m/h/d → seconds.
    m = _EVERY_RE.match(schedule)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = {
            "s": 1, "m": 60, "h": 3600, "d": 86400,
        }.get(unit, 0)
        if seconds <= 0 or n <= 0:
            return None
        return after + n * seconds

    return None


def validate_schedule(schedule: str) -> str | None:
    """Return None when valid, otherwise an error message for the CLI."""
    if next_due_at(schedule, after=time.time(), created_at=time.time()) is None:
        return (
            f"Schedule {schedule!r} not understood. Use one of:\n"
            "  @hourly\n"
            "  @daily HH:MM\n"
            "  @every <N><s|m|h|d>"
        )
    return None


# --- store I/O ----------------------------------------------------------


def load_jobs(home: Path) -> list[JobSpec]:
    path = jobs_file(home)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [JobSpec.from_dict(d) for d in raw if isinstance(d, dict)]


def save_jobs(jobs: list[JobSpec], home: Path) -> None:
    path = jobs_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([j.to_dict() for j in jobs], indent=2, ensure_ascii=False)
    )


def add_job(home: Path, job: JobSpec) -> None:
    jobs = load_jobs(home)
    jobs.append(job)
    save_jobs(jobs, home)


def remove_job(home: Path, job_id: str) -> bool:
    """Remove by id OR prefix. Returns True iff exactly one match."""
    jobs = load_jobs(home)
    matches = [j for j in jobs if j.id == job_id or j.id.startswith(job_id)]
    if len(matches) != 1:
        return False
    remaining = [j for j in jobs if j.id != matches[0].id]
    save_jobs(remaining, home)
    return True


# --- tick logic ---------------------------------------------------------


def due_jobs(jobs: list[JobSpec], *, now: float | None = None) -> list[JobSpec]:
    """Return the subset of enabled jobs whose next_due_at <= now.

    This is pure: it doesn't mark anything as run. The caller (CLI
    `cron tick`) does the actual ask execution and writes back
    last_run_at on success.
    """
    now = now if now is not None else time.time()
    due: list[JobSpec] = []
    for job in jobs:
        if not job.enabled:
            continue
        anchor = job.last_run_at or job.created_at
        nd = next_due_at(
            job.schedule, after=anchor, created_at=job.created_at
        )
        if nd is None:
            continue
        if nd <= now:
            due.append(job)
    return due
