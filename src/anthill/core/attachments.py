"""@file / @glob — files as first-class prompt context (v0.1.11+).

The user types ``@path/to/file.py`` (or a glob like ``@src/**/*.py``)
and the referenced files are read, formatted into an attachment block,
and prepended to the prompt. The original ``@`` token stays in the
visible request so the user can recall what they meant — what the
model sees has the actual file contents inlined above it.

Design choices (and why):

- **Whitespace-delimited tokens.** A token is ``@`` followed by any
  run of non-whitespace. Punctuation inside a path is rare enough
  that the simplest tokenizer beats a regex that tries to be smart
  about trailing commas. If your filename has a comma, quote-escape
  via shell or just don't put trailing comma right after the token.

- **Glob via pathlib.Path.glob.** ``**`` works recursively for any
  pattern starting with ``**/`` or containing ``/**/``. No third-party
  glob library; ``pathlib`` covers our needs.

- **Per-file size cap (default 100 KB).** Real source files rarely
  exceed this. Large files (logs, data dumps) almost always carry a
  payload too long for context anyway — we'd rather emit a warning
  than silently blow up the prompt.

- **Total cap (default 500 KB).** Once aggregate attachments cross
  this, the rest are skipped with a warning. Prevents a stray
  ``@*.log`` from filling the context window in one step.

- **Binary detection.** A NUL byte in the first 1 KiB ⇒ skip. We are
  a text-orchestration tool; binary files can't help the model and
  pasting their bytes wastes tokens.

- **Errors are warnings, not exceptions.** Missing files / unreadable
  paths emit an ``AttachmentError`` in the result list but the parse
  itself never raises. Callers (REPL, CLI) decide what to show.

The returned ``AttachmentBlock`` carries both the rendered text
(ready to prepend to the prompt) and the per-file metadata so the
REPL can echo ``📎 attached <n> file(s)`` after a successful expand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# A token is "@" followed by any run of non-whitespace, non-"@" chars.
# We allow a few trailing punctuation characters to terminate cleanly
# (",.;:!?)"]") so the user can write '@foo.py, and then @bar.py'.
_AT_TOKEN_RE = re.compile(r"@([^\s@]+)")
_TRIM_TRAILING = ",.;:!?)]}"

# Glob metacharacters that trigger pathlib.glob expansion. If none of
# these appear in the token, we treat it as a literal path.
_GLOB_CHARS = set("*?[")

# Defaults; can be overridden by callers (e.g. config files in 0.1.x+).
DEFAULT_PER_FILE_CAP_BYTES = 100 * 1024
DEFAULT_TOTAL_CAP_BYTES = 500 * 1024


@dataclass
class AttachedFile:
    """One successfully read file."""

    path: str         # the path as the user would recognize it (relative if possible)
    content: str
    size_bytes: int


@dataclass
class AttachmentError:
    """One thing that went wrong while expanding @-tokens."""

    token: str        # the raw "@..." the user typed
    reason: str       # human-readable: "not found", "binary file", "too large", etc.


@dataclass
class AttachmentBlock:
    """The outcome of expanding all @-tokens in a request."""

    files: list[AttachedFile] = field(default_factory=list)
    errors: list[AttachmentError] = field(default_factory=list)
    truncated: bool = False  # True when total cap kicked in mid-expand

    def render(self) -> str:
        """Format the attachments as a context block to prepend to the prompt.

        Empty when no files were successfully read — callers should
        skip the prepend entirely rather than insert an empty header.
        """
        if not self.files:
            return ""
        parts = ["[attached files — read these before answering]\n"]
        for f in self.files:
            parts.append(f"<file path={f.path!r}>\n{f.content}\n</file>\n")
        return "".join(parts) + "\n"


def parse_at_tokens(text: str) -> list[str]:
    """Find every ``@<token>`` in the text.

    Returns the raw tokens *without* the leading ``@``. Trailing
    punctuation that's clearly not part of a filename (``,.;:!?)]}``)
    is stripped — so ``@foo.py,`` yields ``foo.py``.
    """
    out: list[str] = []
    for match in _AT_TOKEN_RE.finditer(text):
        token = match.group(1)
        # Trim run of trailing punctuation.
        while token and token[-1] in _TRIM_TRAILING:
            token = token[:-1]
        if token:
            out.append(token)
    return out


def _looks_binary(data: bytes) -> bool:
    """Heuristic — a NUL byte in the first 1 KiB strongly suggests binary."""
    return b"\x00" in data[:1024]


def _resolve_paths(token: str, base: Path) -> list[Path]:
    """Expand one @-token into 0+ filesystem paths.

    Glob patterns expand against ``base``; literal paths resolve
    relative to it (or absolute if the user typed an absolute path).
    Returns paths sorted for deterministic ordering — useful for
    cache keys and test stability.
    """
    if any(ch in token for ch in _GLOB_CHARS):
        # pathlib.Path.glob doesn't accept absolute patterns; if the
        # user wrote /abs/path/**/*.py we anchor at the root.
        raw = Path(token)
        if raw.is_absolute():
            anchor = Path(raw.anchor)
            pattern = str(raw.relative_to(anchor))
            matches = list(anchor.glob(pattern))
        else:
            matches = list(base.glob(token))
        return sorted(p for p in matches if p.is_file())
    raw_path = Path(token)
    candidate = raw_path if raw_path.is_absolute() else (base / raw_path)
    if candidate.is_file():
        return [candidate]
    return []


def _read_text(
    path: Path,
    per_file_cap: int,
) -> tuple[str | None, str | None]:
    """Read one file. Returns (text, error_reason). Exactly one is None."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return None, f"read failed: {e}"
    if _looks_binary(data):
        return None, "binary file"
    if len(data) > per_file_cap:
        return None, (
            f"too large ({len(data):,} bytes; cap is "
            f"{per_file_cap:,})"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Last-ditch: decode with replacement so the model gets
        # *something* readable instead of a hard error on near-utf8 files.
        text = data.decode("utf-8", errors="replace")
    return text, None


def expand_attachments(
    request: str,
    *,
    base: Path | None = None,
    per_file_cap: int = DEFAULT_PER_FILE_CAP_BYTES,
    total_cap: int = DEFAULT_TOTAL_CAP_BYTES,
) -> AttachmentBlock:
    """Parse and resolve every ``@``-token in ``request``.

    ``base`` is the directory to resolve relative paths against —
    typically the REPL's working directory. Defaults to ``Path.cwd()``.

    Caps are enforced per-file (hard skip with an error) and in
    aggregate (later files skipped once the total goes over). De-dup
    is done by resolved absolute path — the same file referenced by
    two tokens is read once.
    """
    base = base or Path.cwd()
    tokens = parse_at_tokens(request)
    block = AttachmentBlock()
    if not tokens:
        return block

    total_so_far = 0
    seen: set[Path] = set()
    for token in tokens:
        paths = _resolve_paths(token, base)
        if not paths:
            block.errors.append(AttachmentError(token=f"@{token}", reason="not found"))
            continue
        for path in paths:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            seen.add(resolved)

            text, err = _read_text(path, per_file_cap)
            if err is not None or text is None:
                block.errors.append(
                    AttachmentError(token=f"@{token}", reason=err or "unknown error")
                )
                continue

            size = len(text.encode("utf-8"))
            if total_so_far + size > total_cap:
                block.truncated = True
                block.errors.append(
                    AttachmentError(
                        token=f"@{token}",
                        reason=(
                            f"skipped — total attachment cap "
                            f"({total_cap:,} bytes) would be exceeded"
                        ),
                    )
                )
                # Don't even try later tokens once we've hit the cap.
                return block
            total_so_far += size

            # Display path: relative to base if possible, else absolute.
            try:
                display = str(path.relative_to(base))
            except ValueError:
                display = str(path)
            block.files.append(
                AttachedFile(path=display, content=text, size_bytes=size)
            )
    return block
