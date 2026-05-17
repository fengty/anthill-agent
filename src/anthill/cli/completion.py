"""0.1.14 — Tab completion for the REPL.

Two-layer design:

1. ``ReplCompleter`` is the testable, deterministic completion engine.
   Given a buffer + cursor position it returns the list of candidate
   completions for whatever's under the cursor. It knows about:
     - slash commands (the keys of HELP_TEXT)
     - subcommands of common slashes (`/model`, `/rate`, `/plan`)
     - configured model names (via ``UserConfig.models``)
     - nation names on disk
     - ``@``-token file paths (cwd-relative, glob-aware)

2. ``install_readline_completion()`` wires the engine into Python's
   ``readline`` so Tab in the live REPL triggers it. POSIX-only; on
   platforms without readline this is a silent no-op (matching the
   existing arrow-key plumbing).

Keep #1 pure so tests don't need a real terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Slash commands that take a sub-argument the completer can help with.
# Maps slash name to a function ``() -> list[str]`` that yields the
# legal sub-arguments at that moment. The function is recomputed on
# every Tab so freshly-added models / nations show up immediately.
SLASH_SUBARGS_FACTORY = "_lazy_subargs"  # implementation detail; see ReplCompleter


@dataclass
class CompletionContext:
    """What the completer needs to know about the world.

    Decoupled from globals so tests can construct one with any
    combination of model names / nation names / cwd.
    """

    slash_commands: tuple[str, ...]
    model_names: tuple[str, ...]
    nation_names: tuple[str, ...]
    cwd: Path

    @classmethod
    def from_runtime(cls) -> "CompletionContext":
        """Build a context by inspecting the live config + filesystem.

        Imported lazily so ``ReplCompleter`` itself stays cheap to
        construct in unit tests that don't need full runtime state.
        """
        from anthill.config import AnthillConfig
        from anthill.core.userconfig import load_config

        cfg = load_config()
        anthill_cfg = AnthillConfig.load()
        nations_dir = anthill_cfg.home / "nations"
        if nations_dir.exists():
            nation_names = tuple(
                sorted(p.name for p in nations_dir.iterdir() if p.is_dir())
            )
        else:
            nation_names = ()
        return cls(
            slash_commands=KNOWN_SLASH_COMMANDS,
            model_names=tuple(sorted(m.name for m in cfg.models)),
            nation_names=nation_names,
            cwd=Path.cwd(),
        )


# Single source of truth for the slash-command vocabulary. Order
# doesn't matter — we sort at completion time so output is stable.
KNOWN_SLASH_COMMANDS: tuple[str, ...] = (
    "/help", "/?", "/quit", "/q", "/exit",
    "/clear", "/status",
    "/trails", "/identity", "/power", "/history", "/project", "/skills",
    "/citizens", "/citizen",
    "/rate", "/model", "/nation", "/plan", "/setup",
)


class ReplCompleter:
    """Pure completion engine. No readline, no globals — testable."""

    def __init__(self, ctx: CompletionContext) -> None:
        self.ctx = ctx

    # ---- Public API ----

    def complete(self, buffer: str, cursor: int | None = None) -> list[str]:
        """Return candidate completions for the token under the cursor.

        ``buffer`` is the full line; ``cursor`` is the byte index where
        the user's caret is. When omitted, treats the cursor as
        end-of-buffer (the common Tab-at-the-end case).

        Returns the full completion strings (not just the suffix) so
        the caller can do whatever rendering it wants. Empty list
        means "nothing to complete".
        """
        if cursor is None:
            cursor = len(buffer)
        prefix, token = _split_token(buffer, cursor)

        # @file token wins regardless of position — it's content, not command.
        if token.startswith("@"):
            return self._complete_attachment(token)

        # Slash command space: at the very start of the buffer, or
        # right after we already typed `/something `.
        head = prefix.lstrip()
        if not head and token.startswith("/"):
            return self._complete_slash(token)

        # Slash sub-argument space: buffer starts with /word and we're
        # after the space.
        if head.startswith("/") and " " in prefix:
            slash_name = head.split(" ", 1)[0]
            return self._complete_slash_subarg(slash_name, token)

        return []

    # ---- Internals ----

    def _complete_slash(self, partial: str) -> list[str]:
        return sorted(
            cmd for cmd in self.ctx.slash_commands if cmd.startswith(partial)
        )

    def _complete_slash_subarg(self, slash: str, partial: str) -> list[str]:
        candidates: tuple[str, ...] = ()
        # /model use|rm <name>; /model <subcommand> too
        if slash in ("/model",):
            # Offer the verbs + every configured model name. The
            # in-REPL handler accepts the name as the second arg of
            # either "use NAME" or "rm NAME", so giving the names
            # directly works for both code paths.
            candidates = self.ctx.model_names + (
                "use", "list", "rm", "remove", "add", "test",
            )
        elif slash == "/nation":
            candidates = self.ctx.nation_names
        elif slash == "/rate":
            candidates = ("up", "down")
        elif slash == "/plan":
            candidates = ("on", "off")
        elif slash in ("/citizens", "/citizen"):
            candidates = ("migrate", "migrate-all", "fix", "fix-all")
        return sorted(c for c in candidates if c.startswith(partial))

    def _complete_attachment(self, token: str) -> list[str]:
        """Resolve cwd-relative paths for the ``@``-prefix syntax.

        Treat the token as ``@<partial-path>`` and offer matching files
        and directories. Directories get a trailing ``/`` so the user
        can keep tabbing into them.
        """
        raw = token[1:]  # strip leading '@'
        path = Path(raw) if raw else Path()
        # When the user typed a partial filename, completion is "list
        # the parent directory and filter by prefix". When they typed
        # only a directory and a trailing /, list its contents.
        if raw.endswith("/") or raw == "":
            base = self.ctx.cwd / raw if raw else self.ctx.cwd
            prefix = ""
        else:
            base = self.ctx.cwd / (path.parent if path.parent != Path() else Path())
            prefix = path.name

        if not base.is_dir():
            return []

        out: list[str] = []
        try:
            for child in sorted(base.iterdir()):
                if child.name.startswith("."):
                    continue
                if not child.name.startswith(prefix):
                    continue
                # Reconstruct a token relative to cwd, preserving the
                # path-prefix the user already typed.
                rel = child.relative_to(self.ctx.cwd)
                suffix = "/" if child.is_dir() else ""
                out.append(f"@{rel}{suffix}")
        except OSError:
            return []
        return out


def _split_token(buffer: str, cursor: int) -> tuple[str, str]:
    """Split ``buffer`` at ``cursor``; return (prefix-before-token, token).

    The token is everything from the last whitespace up to the cursor.
    ``@`` is treated as part of the token, not a delimiter — that's
    what makes the ``@file`` completion work mid-sentence.
    """
    upto = buffer[:cursor]
    # Find the last whitespace before the cursor.
    for i in range(len(upto) - 1, -1, -1):
        if upto[i].isspace():
            return upto[: i + 1], upto[i + 1 :]
    return "", upto


# ---------------------------------------------------------------------------
# Readline glue
# ---------------------------------------------------------------------------


def install_readline_completion() -> bool:
    """Wire the completer into the live readline session. Idempotent.

    Returns True when readline was available and the completer was
    installed; False on platforms without readline (vanilla Windows)
    or when something went wrong. The REPL treats both cases as "no
    Tab completion, everything else works."
    """
    try:
        import readline  # noqa: PLC0415 — optional module
    except ImportError:
        return False

    # Build the completer lazily so tests can stub the runtime config
    # without monkeypatching this module.
    def _completer(text: str, state: int) -> str | None:
        try:
            ctx = CompletionContext.from_runtime()
        except Exception:  # noqa: BLE001 — completion must not crash
            return None
        engine = ReplCompleter(ctx)
        buffer = readline.get_line_buffer()
        cursor = readline.get_endidx()
        candidates = engine.complete(buffer, cursor)
        if state < len(candidates):
            # Return the suffix of the candidate after the current
            # token start — readline replaces the in-progress token
            # with whatever we return.
            _, token = _split_token(buffer, cursor)
            cand = candidates[state]
            # When the candidate already starts with the token, hand
            # back just the unmatched suffix so readline appends.
            if cand.startswith(token):
                return cand
            return cand
        return None

    readline.set_completer(_completer)
    # On macOS the system readline is actually libedit, which uses a
    # different config syntax. Bind both forms so Tab works either way.
    try:
        if "libedit" in readline.__doc__ or "":  # type: ignore[operator]
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
    except (AttributeError, TypeError):
        readline.parse_and_bind("tab: complete")
    # `@` and `/` must not act as token delimiters or our split logic
    # disagrees with readline's. Default delims include them.
    try:
        delims = readline.get_completer_delims()
        for ch in "@/":
            delims = delims.replace(ch, "")
        readline.set_completer_delims(delims)
    except AttributeError:
        pass
    return True
