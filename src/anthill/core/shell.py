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


# Match [[bash:CMD]] including newlines inside the command.
# We use `.*?` (NOT `.+?`) so an empty body `[[bash:]]` matches
# zero chars and the regex stops there. With `.+?` the regex would
# backtrack and greedy-extend across the next REAL marker, eating
# everything in between. Empty bodies are then filtered in
# extract_bash_blocks (you can't run nothing).
_BASH_MARKER_RE = re.compile(
    r"\[\[\s*bash\s*:\s*(?P<cmd>.*?)\s*\]\]",
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


# Markdown bash fence: ```bash\nCMD\n``` (also accepts ```sh /
# ```shell, or no lang). Multi-line bodies match; the caller decides
# what to do with them.
_BASH_FENCE_RE = re.compile(
    r"```(?:bash|sh|shell)?\s*\n(?P<body>.*?)\n```",
    re.IGNORECASE | re.DOTALL,
)


def build_interpretation_prompt(
    user_question: str,
    runs: list[tuple[str, "ShellResult"]],
) -> str:
    """0.2.24 — assemble a prompt asking the model to interpret
    shell outputs against the user's question.

    `runs` is a list of (command_as_emitted, ShellResult) tuples in
    execution order. We feed stdout + exit code + stderr (truncated
    for prompt budget) and ask for a tight 1-2 sentence read.
    """
    parts = [f"The user asked: {user_question.strip()}", ""]
    parts.append("I ran these commands and got these results:")
    for cmd, res in runs:
        parts.append("")
        parts.append(f"$ {res.command}")
        # Cap stdout to keep the interp prompt cheap.
        if res.stdout.strip():
            parts.append(res.stdout[:1500].rstrip())
        else:
            parts.append("(no stdout)")
        if res.stderr.strip():
            parts.append(f"stderr: {res.stderr[:500].rstrip()}")
        if res.timed_out:
            parts.append(f"(TIMED OUT after {res.duration_seconds:.1f}s)")
        else:
            parts.append(f"(exit {res.returncode})")
    parts.append("")
    parts.append(
        "Now give a 1-2 sentence interpretation of what these "
        "results mean FOR THE USER'S QUESTION. Plain prose. No "
        "markup, no preamble, no 'based on the output above', no "
        "list of what each command did — just the practical "
        "answer they were looking for."
    )
    return "\n".join(parts)


def extract_fence_candidates(text: str) -> list[str]:
    """0.2.23 — find shell-command candidates in `````bash````` fences.

    Called when the model wrote markdown fences instead of using the
    [[bash:CMD]] marker — common LLM regression we can recover from.
    Returns the inner commands trimmed; multi-line bodies skipped
    (those are more likely tutorial code than 'run this').

    Empty list when:
      - No fences found
      - The output already has [[bash:]] markers (model did the
        right thing somewhere; assume it was deliberate elsewhere)
      - All fence bodies are multi-line / empty / too long
    """
    # If the output uses the proper marker anywhere, don't second-
    # guess — assume the fence was intentional explanation.
    if extract_bash_blocks(text):
        return []
    candidates: list[str] = []
    for m in _BASH_FENCE_RE.finditer(text):
        body = m.group("body").strip()
        if not body:
            continue
        if "\n" in body:
            continue  # multi-line: likely a script, not a one-shot
        if len(body) > 200:
            continue
        candidates.append(body)
    return candidates


# --- citizen prompt addition -----------------------------------------


SHELL_TOOL_INSTRUCTION = """\
==================
SHELL TOOL — READ THIS CAREFULLY:

When the king asks you to actually DO something on their machine
(ping a host, check git status, look at a file, list processes,
curl an endpoint, etc.), you have TWO choices:

  ✓ CORRECT — emit a runnable marker:
    [[bash:ping -c 5 192.168.1.149]]

  ✗ WRONG — emit a markdown code fence:
    ```bash
    ping -c 5 192.168.1.149
    ```

The CORRECT form ACTUALLY EXECUTES on the king's shell and shows
real output. The WRONG form is just static text the king has to
copy-paste themselves — useless when they asked you to do it for
them. ALWAYS prefer [[bash:CMD]] when the king wants action.

When the king types a literal shell command (e.g. "ping
192.168.1.149", "git status", "df -h"), they ALREADY know what to
type — they want to SEE THE RESULT. Run it: [[bash:ping
192.168.1.149]]. Don't restate the command in prose, don't add
explanation about flags, don't offer "想展开告诉我" — they want
output, not tutorials.

Examples:
  King: ping 192.168.1.149
  You:  [[bash:ping -c 10 192.168.1.149]]

  King: git 当前 branch?
  You:  [[bash:git branch --show-current]]

  King: 磁盘还剩多少
  You:  [[bash:df -h]]

Multiple blocks in one response are fine — they run in order. You
CAN narrate around them ("checking connectivity: [[bash:ping...]] —
if 0% loss we're good").

DON'T emit [[bash:...]] for destructive commands (rm -rf, dd, mkfs,
sudo destructive things) without first asking the king to confirm.

If a 30s timeout would be too short, ALSO say so in plain text so
the king knows to /loop or /retry with a different cap.
=================="""


# --- fast-path: literal shell command detection -----------------------
#
# When the king types a string that is OBVIOUSLY a shell command
# (`ping 192.168.1.149`, `git status`, `df -h`), there's no reason
# to send it through Scout + a citizen + the marker dance. We just
# run it. Zero LLM cost, sub-second response.
#
# The KNOWN_COMMANDS list is intentionally conservative — we'd
# rather miss a fast-path opportunity than mis-execute a question.
# A user can always force fast-path by prefixing with `!` or `$`.

_KNOWN_COMMANDS: frozenset[str] = frozenset({
    # Networking
    "ping", "ping6", "traceroute", "tracepath", "tracert", "mtr",
    "dig", "nslookup", "host", "whois",
    "netstat", "ss", "ifconfig", "ip", "route", "arp",
    "curl", "wget", "http", "httpie",
    "telnet", "nc", "ncat", "nmap",
    # File / dir inspection (read-only-ish)
    "ls", "ll", "la", "tree", "find", "locate",
    "cat", "less", "more", "head", "tail",
    "stat", "file", "wc", "du", "df", "mount", "lsblk",
    # Process / system
    "ps", "top", "htop", "btop", "free", "uptime", "w", "who",
    "lsof", "kill",  # kill needs args; user is explicit
    "uname", "hostname", "whoami", "id", "groups",
    "date", "cal", "tty",
    # Version control
    "git", "hg", "svn",
    # Build / package
    "make", "cmake", "ninja",
    "npm", "yarn", "pnpm", "bun",
    "pip", "pip3", "poetry", "uv",
    "cargo", "rustup",
    "go",
    "mvn", "gradle",
    "brew", "apt", "apt-get", "yum", "dnf", "pacman",
    # Container / cloud
    "docker", "podman",
    "kubectl", "helm",
    "terraform",
    "gh", "glab",
    # Languages (REPL-friendly one-liners)
    "python", "python3", "node", "deno", "ruby", "perl", "php", "lua",
    # Text processing
    "grep", "egrep", "fgrep", "rg", "ack", "ag",
    "awk", "sed", "tr", "cut", "sort", "uniq",
    "diff", "cmp", "patch",
    "jq", "yq", "xmllint",
    # Misc
    "echo", "printf", "true", "false",
    "which", "whereis", "type", "command",
    "env", "printenv", "export",  # export rarely useful as one-shot
    "history",  # shell history, not our /history
    "man", "info", "tldr",
    "tar", "gzip", "gunzip", "zip", "unzip", "xz",
    "base64", "md5", "md5sum", "sha1sum", "sha256sum",
    "openssl",
    "ssh", "scp", "rsync",  # ssh blocks interactively; warn handled elsewhere
})


# Patterns that suggest a question, not a command. If ANY appear, we
# refuse the fast path and let the LLM handle it (the user might be
# asking "ping 192.168.1.149 通吗？" which DOES need explanation).
_QUESTION_MARKERS = (
    "?", "？",
    "怎么", "如何", "为什么", "什么是", "是什么", "为啥",
    "how do", "how to", "what is", "what's", "why", "should i",
    "通吗", "好不好", "可以吗", "对不对",
)


def looks_like_shell_command(text: str) -> str | None:
    """Detect input that is a literal shell command.

    Returns the cleaned command (stripped of wrappers) if it's a
    direct command we can fast-path. Returns None if it's prose, a
    question, or anything we should send through the LLM.

    Heuristics, in order:
      1. Strip wrappers: backticks, `$ ` prefix, `! ` prefix, code
         fence with one bash line inside
      2. Reject anything multi-paragraph or > 200 chars
      3. Reject if it contains question particles ("?", "怎么", ...)
      4. Accept if first token is in _KNOWN_COMMANDS
      5. Accept if input starts with `!` (explicit user opt-in:
         `! anyrandomcmd here` always runs)

    The `!` opt-in is the escape hatch: power users who want to run
    something we don't have in KNOWN_COMMANDS just prefix it.
    """
    if not text:
        return None
    s = text.strip()
    if not s:
        return None

    # 1a. Explicit opt-in: `!cmd args` or `! cmd args` always runs.
    if s.startswith("!"):
        return s[1:].lstrip() or None

    # 1b. Strip a `$` prompt-style prefix.
    if s.startswith("$ "):
        s = s[2:]
    elif s.startswith("$") and len(s) > 1 and s[1] != "{":
        s = s[1:].lstrip()

    # 1c. Code fence first (so we don't eat the outer triples as
    # individual backticks below). Single-line bash/sh fence:
    # ```bash\ncmd\n```
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            body = "\n".join(lines[1:-1]).strip()
            if body and "\n" not in body:
                s = body

    # 1d. Inline backtick wrappers (single backtick each side).
    if (
        s.startswith("`")
        and s.endswith("`")
        and not s.startswith("```")
        and len(s) >= 2
    ):
        s = s[1:-1].strip()

    if not s:
        return None

    # 2. Length / paragraph cap. Real commands are short and one-line.
    if len(s) > 200 or "\n" in s:
        return None

    # 3. Question particles.
    low = s.lower()
    if any(q in low for q in _QUESTION_MARKERS):
        return None

    # 4. First token must be a known command.
    first = s.split(maxsplit=1)[0]
    # Strip optional leading "sudo" — we treat it as a transparent
    # wrapper; the actual command is the next token.
    if first == "sudo":
        tokens = s.split(maxsplit=2)
        if len(tokens) >= 2 and tokens[1] in _KNOWN_COMMANDS:
            return s
        return None
    if first in _KNOWN_COMMANDS:
        return s

    return None
