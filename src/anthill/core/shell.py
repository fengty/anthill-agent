"""0.2.19 — Shell execution primitive.

The fundamental gap before this version: anthill could TALK about
commands but couldn't RUN them. A user asking "持续 ping 看是否
丢包 ping 192.168.1.149" got a tutorial on what ping does instead
of an actual ping result.

Design (deliberately minimal):

  - Citizens emit `[[bash:CMD]]` markers in their final output
  - REPL detects + executes + renders the output
  - We auto-inject sane caps (ping -c 10 instead of indefinite)
  - 30s hard timeout per command — long-running tasks need /loop
  - No sandbox: this runs on the king's machine as the king's user.
    "用户是国王" is the model: we trust them, but apply defensive
    bounded execution.

What we deliberately DON'T do (yet):
  - Sandboxing / containers (hermes does this; overkill for local use)
  - Tool-use API native (provider-specific; markers work everywhere)
  - Per-command approval (friction; let the king say /noexec to opt out)

Safety:
  - The HARD-DENY list catches the few patterns that are catastrophic
    by mistake (rm -rf /, fork bombs, mkfs to a device, etc.)
  - Auto-cap injection turns `ping <ip>` into `ping -c 10 <ip>` so a
    forgotten -c doesn't tie the REPL down
  - Timeout is non-negotiable — the user can /retry with a different
    cap if a real long-running command was needed
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional


# Hard timeout for a single shell command. 30s is enough for ping
# -c 10, curl with default timeout, df -h, git status, etc. Beyond
# that, the user can ask for /loop or split the work.
DEFAULT_TIMEOUT_SECONDS: float = 30.0

# Maximum bytes captured from each stream. Protects against
# `find /` or `cat /var/log/*` flooding the REPL.
MAX_CAPTURE_BYTES: int = 64 * 1024


# --- danger detection -------------------------------------------------


# Patterns we hard-refuse without explicit override. These are the
# "obviously wrong" ones — irreversible, fast, and unambiguous. We
# DO NOT try to catch every dangerous command; users have rm, sudo,
# and a million ways to break their machine. We block only the
# patterns that are pure footguns.
_DANGER_PATTERNS = (
    # Recursive force delete of root or home.
    re.compile(r"\brm\s+(-[a-zA-Z]*[rRf][a-zA-Z]*\s+)+(/\s|/$|~\s|~$|/\*)"),
    # Classic fork bomb.
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:"),
    # mkfs / dd on a raw device.
    re.compile(r"\bmkfs(\.[a-z0-9]+)?\s+/dev/"),
    re.compile(r"\bdd\s+.*\bof=/dev/(sd|nvme|disk|hd)"),
    # Pipe the whole disk to /dev/null intentionally.
    re.compile(r"\b(shred|wipe)\s+/dev/"),
    # chmod / chown 777 on root.
    re.compile(r"\bchmod\s+-R\s+\d+\s+/(\s|$)"),
)


def is_dangerous(cmd: str) -> str | None:
    """Return a short reason if `cmd` matches a hard-deny pattern, else None."""
    s = cmd.strip()
    for pat in _DANGER_PATTERNS:
        if pat.search(s):
            return f"matches hard-deny pattern: {pat.pattern[:50]}..."
    return None


# --- auto-cap injection -----------------------------------------------


def apply_caps(cmd: str) -> str:
    """Inject sane caps for commands that otherwise run indefinitely.

    Currently handled:
      - `ping <host>` (no -c) → `ping -c 10 <host>`
      - `tail -f X` → unchanged (user explicitly asked for follow)
      - `curl <url>` (no --max-time) → `curl --max-time 20 <url>`

    Returns the modified command. When no cap applies, returns input
    unchanged.
    """
    stripped = cmd.strip()
    if not stripped:
        return cmd

    # Use shlex to tokenize the FIRST shell statement only (split on ;|&&||)
    # so we don't mangle pipelines. For an MVP we just look at the head
    # of each separately-runnable segment.
    # For now: only inject caps for the simple case where the whole
    # command is one program invocation (no pipes / redirections).
    if any(c in stripped for c in ("|", ";", "&&", "||", ">", "<")):
        return cmd

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return cmd  # unbalanced quotes — let subprocess fail naturally
    if not tokens:
        return cmd

    head = tokens[0].rsplit("/", 1)[-1]  # strip path prefix

    if head == "ping":
        # Inject -c 10 unless already present.
        if not any(t in ("-c", "--count") for t in tokens[1:]):
            return f"ping -c 10 " + " ".join(shlex.quote(t) for t in tokens[1:])

    if head == "curl":
        if not any(t.startswith("--max-time") or t == "-m" for t in tokens[1:]):
            return (
                "curl --max-time 20 "
                + " ".join(shlex.quote(t) for t in tokens[1:])
            )

    return cmd


# --- result type ------------------------------------------------------


@dataclass
class ShellResult:
    """Outcome of one shell command execution."""

    command: str            # the command actually run (after caps)
    returncode: int         # process exit code
    stdout: str             # captured stdout (truncated to MAX_CAPTURE_BYTES)
    stderr: str             # captured stderr (truncated)
    duration_seconds: float
    timed_out: bool
    blocked_reason: Optional[str] = None  # set if we refused to run

    @property
    def ok(self) -> bool:
        return self.blocked_reason is None and not self.timed_out and self.returncode == 0

    @property
    def short_summary(self) -> str:
        """One-line summary for log lines / status displays."""
        if self.blocked_reason:
            return f"blocked: {self.blocked_reason}"
        if self.timed_out:
            return f"timed out after {self.duration_seconds:.1f}s"
        if self.returncode == 0:
            return f"ok ({self.duration_seconds:.2f}s)"
        return f"exit {self.returncode} ({self.duration_seconds:.2f}s)"


# --- runner ----------------------------------------------------------


def safe_run(
    cmd: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    allow_dangerous: bool = False,
    cwd: str | None = None,
) -> ShellResult:
    """Run `cmd` through the user's shell with safety guards.

    Steps:
      1. is_dangerous check — refuse unless allow_dangerous=True
      2. apply_caps — inject sane defaults for unbounded commands
      3. subprocess.run with shell=True, captured stdout/stderr
      4. Hard timeout via subprocess timeout

    Returns a ShellResult regardless of success/failure. Never raises
    for command-level errors; only raises if the python-level
    subprocess invocation itself fails catastrophically.
    """
    started = time.perf_counter()

    # 1. Danger gate.
    danger = is_dangerous(cmd) if not allow_dangerous else None
    if danger:
        return ShellResult(
            command=cmd,
            returncode=-1,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            timed_out=False,
            blocked_reason=danger,
        )

    # 2. Cap injection.
    runnable = apply_caps(cmd)

    # 3. Execute.
    timed_out = False
    try:
        proc = subprocess.run(
            runnable,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        returncode = proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        # `e.stdout` / `e.stderr` are bytes on timeout; decode safely.
        stdout = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or "")
        stderr = (e.stderr or b"").decode("utf-8", errors="replace") if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or "")
        returncode = -1
    except OSError as e:
        return ShellResult(
            command=runnable,
            returncode=-1,
            stdout="",
            stderr=str(e),
            duration_seconds=time.perf_counter() - started,
            timed_out=False,
        )

    duration = time.perf_counter() - started

    # 4. Truncate captures.
    if len(stdout) > MAX_CAPTURE_BYTES:
        stdout = stdout[:MAX_CAPTURE_BYTES] + "\n…[truncated]"
    if len(stderr) > MAX_CAPTURE_BYTES:
        stderr = stderr[:MAX_CAPTURE_BYTES] + "\n…[truncated]"

    return ShellResult(
        command=runnable,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=duration,
        timed_out=timed_out,
    )


# --- marker extraction ------------------------------------------------


# Match [[bash:CMD]] including newlines inside the command. The
# command captures GREEDILY-up-to-]] so multi-line bash blocks work
# (heredocs, escaped newlines, etc.). We allow optional whitespace.
_BASH_MARKER_RE = re.compile(
    r"\[\[\s*bash\s*:\s*(?P<cmd>.+?)\s*\]\]",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class BashBlock:
    """One [[bash:...]] marker found in model output."""

    start: int          # offset in original text
    end: int
    command: str        # the trimmed command


def extract_bash_blocks(text: str) -> list[BashBlock]:
    """Return all [[bash:CMD]] blocks in order, non-overlapping.

    The model can include multiple per response. We respect order so
    the REPL can stream "narration → exec → narration → exec" the way
    the citizen wrote it.
    """
    blocks: list[BashBlock] = []
    for m in _BASH_MARKER_RE.finditer(text):
        cmd = m.group("cmd").strip()
        if cmd:
            blocks.append(BashBlock(start=m.start(), end=m.end(), command=cmd))
    return blocks


def strip_bash_blocks(text: str) -> str:
    """Return `text` with every [[bash:CMD]] marker removed.

    Used when we want to show the user the narrative WITHOUT the raw
    marker noise — the REPL replaces markers with rendered output.
    """
    return _BASH_MARKER_RE.sub("", text).strip()


# --- citizen prompt addition -----------------------------------------


SHELL_TOOL_INSTRUCTION = """\
==================
SHELL TOOL: when the king asks you to actually DO something on
their machine (ping a host, check git status, look at a file, list
processes, curl an endpoint, etc.), do NOT explain what to type.
Instead, emit a marker:

  [[bash:CMD]]

anthill will run CMD on the king's local shell and show them the
output. Examples:

  [[bash:ping -c 5 192.168.1.149]]
  [[bash:git status]]
  [[bash:df -h]]
  [[bash:curl -s https://api.example.com/health]]

Multiple blocks in one response are fine — they run in order. The
output appears in the REPL right where the marker was, so you can
narrate around them ("let me check connectivity: [[bash:ping...]] —
if 0% loss we're good").

Don't emit [[bash:...]] for destructive commands (rm, dd, format,
sudo install, etc.) without first asking the king to confirm.

If a 30s timeout would be too short, ALSO say so in plain text so
the king knows to /loop or /retry with a different cap.
=================="""
