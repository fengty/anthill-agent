"""0.2.47 — Client-style paste-to-file in the REPL.

User pain: pasting a wall of text (200-line log, 500-line JSON,
big screenshot of code) into the REPL prompt = visual disaster +
context bloat. Web clients (ChatGPT / Claude.ai) auto-detect heavy
pastes and turn them into attached files. anthill should too.

Two cases handled here:

  1. HEAVY PASTE (>= 1000 chars OR >= 15 lines):
     Save the paste to ~/.anthill/pastes/<ts>-<hash><ext>, rewrite
     the user's input to `@<path>`. The existing @file attachment
     machinery (0.1.11+) picks it up downstream.

  2. PASTED FILE PATH (single line, looks like a path, file exists):
     User drags a file from Finder into the terminal (which inserts
     the absolute path) → auto `@`-prefix so the file gets attached
     instead of being interpreted as a text request.

The detection thresholds are conservative — a 200-char one-liner
doesn't trigger the file save, only genuine "I just dumped my logs"
pastes do. Users who don't want this can /paste off (future flag).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Thresholds: tuned to "this is clearly a heavy paste, not a typed
# message." A typical typed question is <300 chars; a typical
# pasted log/json/code dump is >1000.
PASTE_CHAR_THRESHOLD: int = 1000
PASTE_LINE_THRESHOLD: int = 15


@dataclass
class PasteResult:
    """Outcome of paste detection."""

    rewritten: str       # the new input line (possibly identical)
    persisted_path: Optional[Path] = None
    kind: str = "inline"  # 'inline' / 'paste_saved' / 'path_resolved'
    chars: int = 0
    lines: int = 0


def maybe_persist_paste(line: str, home: Path) -> PasteResult:
    """Detect heavy pastes; save them to disk; rewrite to `@<path>`.

    Returns a PasteResult. When `kind=='inline'` nothing happened
    (the input was short / a slash command / a URL). When
    `kind=='paste_saved'`, the persisted_path is the new file and
    `rewritten` is the `@<path>` form the user effectively typed.
    """
    if not line:
        return PasteResult(rewritten=line, kind="inline")

    # Don't interfere with slash commands or @file references.
    stripped = line.strip()
    if stripped.startswith(("/", "@")):
        return PasteResult(rewritten=line, kind="inline")

    chars = len(line)
    lines = line.count("\n") + 1

    if chars < PASTE_CHAR_THRESHOLD and lines < PASTE_LINE_THRESHOLD:
        return PasteResult(
            rewritten=line, kind="inline", chars=chars, lines=lines,
        )

    # Save it. Hash the content so the same paste shows up at the
    # same filename (debugging convenience).
    h = hashlib.sha1(line.encode("utf-8", errors="replace")).hexdigest()[:8]
    ext = _guess_paste_extension(line)
    paste_dir = Path(home) / "pastes"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = paste_dir / f"{stamp}-{h}{ext}"
    try:
        paste_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(line, encoding="utf-8")
    except OSError:
        # Disk full / readonly / parent-not-a-dir — silently fall
        # back to inline so the user's input still works.
        return PasteResult(
            rewritten=line, kind="inline", chars=chars, lines=lines,
        )

    return PasteResult(
        rewritten=f"@{path}",
        persisted_path=path,
        kind="paste_saved",
        chars=chars,
        lines=lines,
    )


def maybe_resolve_file_path(line: str) -> Optional[Path]:
    """If the line is a single file path the user pasted (e.g. from
    Finder drag-into-terminal), return the resolved Path so caller
    can rewrite as `@<path>`. Returns None otherwise.

    Heuristics for "this is a path, not a question":
      - single line
      - no internal whitespace EXCEPT inside drag-from-Finder paths
        which terminal escapes with backslashes (e.g. `My\\ File.txt`)
      - looks like a path: starts with `/`, `~/`, or `./` (or `C:\\`
        on Windows in future)
      - file actually exists
    """
    s = line.strip()
    if not s or "\n" in s:
        return None
    # Un-escape Finder's "\ " for spaces.
    unescaped = s.replace("\\ ", " ")
    # Path-shaped?
    if not (
        unescaped.startswith("/")
        or unescaped.startswith("~/")
        or unescaped.startswith("./")
    ):
        return None
    p = Path(unescaped).expanduser()
    try:
        if p.exists() and p.is_file():
            return p
    except OSError:
        return None
    return None


# --- helpers -------------------------------------------------------


_JSON_HINT = re.compile(r"^\s*[\{\[]")
_YAML_HINT = re.compile(r"^---|^[a-zA-Z_][\w-]*:\s")
_PY_HINTS = ("def ", "import ", "from ", "class ")
_JS_HINTS = ("function ", "const ", "let ", "var ", "=>")
_HTML_HINTS = ("<!DOCTYPE", "<html", "<head", "<body")
_SQL_HINTS = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE")
_LOG_HINT = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _guess_paste_extension(text: str) -> str:
    """Pick a sensible extension so downstream tools (and the user
    eyeballing ~/.anthill/pastes) can see what they're dealing with.

    Conservative: errs on `.txt` when ambiguous. The extension only
    affects display; the @file attachment reads it as text either way.
    """
    head = text[:500]
    head_lines = head.splitlines()[:5]
    first_nonblank = next((l for l in head_lines if l.strip()), "")

    # JSON: starts with { or [.
    if _JSON_HINT.match(first_nonblank):
        return ".json"
    # Date-stamped log lines.
    if any(_LOG_HINT.match(l) for l in head_lines):
        return ".log"
    # Python.
    if any(hint in head for hint in _PY_HINTS):
        return ".py"
    # JavaScript / TypeScript.
    if any(hint in head for hint in _JS_HINTS):
        return ".js"
    # HTML.
    if any(hint in head for hint in _HTML_HINTS):
        return ".html"
    # SQL.
    if any(hint in head.upper() for hint in _SQL_HINTS):
        return ".sql"
    # YAML.
    if _YAML_HINT.match(first_nonblank):
        return ".yaml"
    return ".txt"
