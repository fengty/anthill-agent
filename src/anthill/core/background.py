"""Background jobs — kick off a long ask, walk away, check back later.

Some asks take minutes. A multi-step research plan with retries on a
slow model can keep the terminal busy for two or three minutes, and
the user might not want to stare at a progress bar for that long.
The fix is the same fix every CLI tool eventually adds: detach.

`anthill bg ask "..."` spawns a child process that runs the normal
ask path, captures its output to a log file, and returns immediately
with a short job_id. `anthill bg list` shows what's running and
finished. `anthill bg show <id>` cat's the accumulated output.
`anthill bg cancel <id>` sends SIGTERM.

State lives under nations/<name>/bg/<job_id>/:
  meta.json      — request, pid, started_at
  output.log     — combined stdout/stderr of the child
  done.json      — written when the child exits (exit_code, completed_at)

The directory itself is the source of truth. Detecting "is this still
running?" combines a cheap aliveness check (`os.kill(pid, 0)` on POSIX)
with the presence of done.json. A missing done.json plus a dead pid
means the process crashed without exiting cleanly — we surface that as
'died' rather than 'completed' so the user notices.

POSIX-only for v0.2.16. Anthill is a developer tool and the bg surface
is mostly for users running long asks on a workstation, not for
production deployments — Windows support can come later if anyone
asks. `start_new_session=True` is the key: it puts the child in its
own process group so killing the parent terminal doesn't take the bg
job down with it.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


JobStatus = Literal["running", "completed", "failed", "died", "cancelled"]


@dataclass
class BackgroundJob:
    """One background ask. Discovered from disk; never instantiated by user."""

    job_id: str
    request: str
    pid: int
    started_at: float
    job_dir: Path
    exit_code: int | None = None
    completed_at: float | None = None
    cancelled: bool = False

    @property
    def log_path(self) -> Path:
        return self.job_dir / "output.log"

    @property
    def is_alive(self) -> bool:
        """POSIX-only check that the recorded pid still exists.

        We don't try to verify it's the SAME process — pid reuse is
        possible if the system has wrapped around, but on the timescales
        a bg ask runs that's essentially never. If you've kept anthill
        bg jobs around for weeks, you have other problems.
        """
        if self.pid <= 0:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False

    @property
    def status(self) -> JobStatus:
        if self.cancelled:
            return "cancelled"
        if self.exit_code is not None:
            return "completed" if self.exit_code == 0 else "failed"
        if self.is_alive:
            return "running"
        return "died"

    @property
    def runtime_seconds(self) -> float:
        end = self.completed_at if self.completed_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def started_at_human(self) -> str:
        return datetime.datetime.fromtimestamp(self.started_at).strftime("%m-%d %H:%M")


# --- paths -----------------------------------------------------------------

def bg_dir(nation_dir: Path) -> Path:
    return nation_dir / "bg"


def job_dir(nation_dir: Path, job_id: str) -> Path:
    return bg_dir(nation_dir) / job_id


def _make_job_id() -> str:
    """8-char prefix is plenty for a few-dozen concurrent jobs per nation."""
    return uuid.uuid4().hex[:8]


# --- spawn -----------------------------------------------------------------

def start_background(
    request: str,
    nation_name: str,
    nation_dir_path: Path,
    *,
    anthill_bin: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> BackgroundJob:
    """Spawn an anthill ask in its own session and return immediately.

    The child writes its own done.json on clean exit (via the
    `_bg_finalize` wrapper) so the parent can detect completion
    without polling for an exit_code we don't have access to.

    Returns the BackgroundJob the parent should hand back to the user
    so they can `bg show <id>` or `bg cancel <id>`.
    """
    job_id = _make_job_id()
    jd = job_dir(nation_dir_path, job_id)
    jd.mkdir(parents=True, exist_ok=True)

    # Use a wrapper that runs `anthill ask` and writes done.json after.
    # The wrapper is a tiny inline shell script so we don't need a new
    # entry point in pyproject.toml just for this.
    binary = anthill_bin or "anthill"
    log_path = jd / "output.log"
    done_path = jd / "done.json"

    # Shell-quote the request once; everything else is trusted.
    import shlex
    quoted_req = shlex.quote(request)
    quoted_nation = shlex.quote(nation_name)
    quoted_done = shlex.quote(str(done_path))
    quoted_log = shlex.quote(str(log_path))

    cmd = (
        f"{binary} ask {quoted_req} --nation {quoted_nation} "
        f"> {quoted_log} 2>&1; "
        f"echo \"{{\\\"exit_code\\\": $?, \\\"completed_at\\\": $(date +%s)}}\" "
        f"> {quoted_done}"
    )

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(
        ["/bin/sh", "-c", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach: closing the parent terminal won't kill it
        env=env,
        cwd=str(nation_dir_path),
    )

    meta = {
        "job_id": job_id,
        "request": request,
        "nation": nation_name,
        "pid": proc.pid,
        "started_at": time.time(),
    }
    (jd / "meta.json").write_text(json.dumps(meta, indent=2))

    return BackgroundJob(
        job_id=job_id,
        request=request,
        pid=proc.pid,
        started_at=meta["started_at"],
        job_dir=jd,
    )


# --- discovery -------------------------------------------------------------

def load_job(job_id: str, nation_dir_path: Path) -> BackgroundJob | None:
    """Prefix match on job_id so the user can type the first few chars."""
    base = bg_dir(nation_dir_path)
    if not base.exists():
        return None
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        if entry.name == job_id or entry.name.startswith(job_id):
            return _read_job(entry)
    return None


def list_jobs(nation_dir_path: Path) -> list[BackgroundJob]:
    """All known bg jobs for the nation, newest first."""
    base = bg_dir(nation_dir_path)
    if not base.exists():
        return []
    jobs: list[BackgroundJob] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        job = _read_job(entry)
        if job is not None:
            jobs.append(job)
    jobs.sort(key=lambda j: j.started_at, reverse=True)
    return jobs


def _read_job(directory: Path) -> BackgroundJob | None:
    meta_path = directory / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    exit_code: int | None = None
    completed_at: float | None = None
    done_path = directory / "done.json"
    if done_path.exists():
        try:
            done = json.loads(done_path.read_text())
            exit_code = int(done.get("exit_code", 0))
            completed_at = float(done.get("completed_at", time.time()))
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    cancelled_marker = directory / "cancelled"
    return BackgroundJob(
        job_id=str(meta.get("job_id", directory.name)),
        request=str(meta.get("request", "")),
        pid=int(meta.get("pid", 0)),
        started_at=float(meta.get("started_at", 0.0)),
        job_dir=directory,
        exit_code=exit_code,
        completed_at=completed_at,
        cancelled=cancelled_marker.exists(),
    )


# --- control ---------------------------------------------------------------

def cancel_job(job_id: str, nation_dir_path: Path) -> bool:
    """SIGTERM the child's whole process group. Returns True if signalled.

    Two failure modes: job doesn't exist (None job), or job already
    finished (no live pid). Both return False. The cancelled marker
    file makes the eventual exit show as 'cancelled' rather than
    'failed' even if the child returned nonzero on the signal.
    """
    job = load_job(job_id, nation_dir_path)
    if job is None or not job.is_alive:
        return False
    try:
        os.killpg(os.getpgid(job.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    (job.job_dir / "cancelled").write_text(str(time.time()))
    return True


def clear_job(job_id: str, nation_dir_path: Path) -> bool:
    """Remove a finished job's directory. Refuses to delete a running one."""
    job = load_job(job_id, nation_dir_path)
    if job is None:
        return False
    if job.status == "running":
        return False
    import shutil
    shutil.rmtree(job.job_dir, ignore_errors=True)
    return True


def read_log(job: BackgroundJob, *, max_bytes: int = 200_000) -> str:
    """Best-effort read of the log file. Truncates to keep terminals happy."""
    if not job.log_path.exists():
        return ""
    raw = job.log_path.read_bytes()
    if len(raw) <= max_bytes:
        return raw.decode("utf-8", errors="replace")
    tail = raw[-max_bytes:]
    return (
        f"[…truncated, showing last {max_bytes:,} bytes…]\n"
        + tail.decode("utf-8", errors="replace")
    )
