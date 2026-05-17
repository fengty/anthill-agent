"""0.1.14 — Tab completion engine tests.

The completer is split from the readline glue: ReplCompleter is pure,
takes a CompletionContext, and returns candidates given a buffer +
cursor. The readline glue is integration-tested manually since it
needs a real terminal.

What we cover:
  1. Slash commands at column 0
  2. Slash subargs (/model, /nation, /rate, /plan)
  3. @file path completion (literal, with dir prefix, dir trailing /)
  4. Dotfiles hidden by default
  5. No completion outside slash/at-token context
  6. Cursor mid-token returns based on what's BEFORE the cursor
"""

from __future__ import annotations

from pathlib import Path


def _ctx(*, models=(), nations=(), cwd=None):
    from anthill.cli.completion import KNOWN_SLASH_COMMANDS, CompletionContext

    return CompletionContext(
        slash_commands=KNOWN_SLASH_COMMANDS,
        model_names=tuple(models),
        nation_names=tuple(nations),
        cwd=cwd or Path("."),
    )


# --- Slash command completion --------------------------------------------


def test_slash_root_completes_prefix() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx())
    assert "/help" in c.complete("/h", 2)
    assert "/history" in c.complete("/h", 2)
    # Filtering: /q should yield /q / /quit / nothing else
    suggestions = c.complete("/q", 2)
    assert set(suggestions) == {"/q", "/quit"}


def test_slash_empty_returns_all() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx())
    suggestions = c.complete("/", 1)
    # Every known slash is a prefix-match for "/"
    assert "/help" in suggestions
    assert "/quit" in suggestions
    assert "/plan" in suggestions


def test_slash_only_at_start_of_buffer() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx())
    # Mid-sentence "/foo" is content, not a command.
    assert c.complete("explain /h", 10) == []


# --- Slash subarg completion ---------------------------------------------


def test_model_subarg_lists_model_names() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx(models=("deepseek", "minimax", "openai")))
    suggestions = c.complete("/model ", 7)
    assert "deepseek" in suggestions
    assert "minimax" in suggestions
    assert "openai" in suggestions
    # Subcommands also appear.
    assert "use" in suggestions


def test_model_subarg_filters_by_prefix() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx(models=("deepseek", "minimax", "openai")))
    suggestions = c.complete("/model d", 8)
    assert suggestions == ["deepseek"]


def test_nation_subarg_lists_nations() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx(nations=("default", "work", "research")))
    suggestions = c.complete("/nation ", 8)
    assert "default" in suggestions
    assert "research" in suggestions
    assert "work" in suggestions


def test_rate_subarg_offers_up_down() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx())
    assert set(c.complete("/rate ", 6)) == {"down", "up"}
    assert c.complete("/rate u", 7) == ["up"]


def test_plan_subarg_offers_on_off() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx())
    assert set(c.complete("/plan ", 6)) == {"off", "on"}


# --- @file completion ----------------------------------------------------


def test_at_completes_top_level_files(tmp_path: Path) -> None:
    from anthill.cli.completion import ReplCompleter

    (tmp_path / "alpha.py").write_text("a")
    (tmp_path / "beta.py").write_text("b")
    (tmp_path / "src").mkdir()
    c = ReplCompleter(_ctx(cwd=tmp_path))
    suggestions = c.complete("explain @a", 10)
    assert suggestions == ["@alpha.py"]


def test_at_completes_with_dir_prefix(tmp_path: Path) -> None:
    from anthill.cli.completion import ReplCompleter

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("a")
    (tmp_path / "src" / "bar.py").write_text("b")
    c = ReplCompleter(_ctx(cwd=tmp_path))
    suggestions = c.complete("@src/f", 6)
    assert suggestions == ["@src/foo.py"]


def test_at_with_trailing_slash_lists_dir(tmp_path: Path) -> None:
    """`@src/` should list everything under src/."""
    from anthill.cli.completion import ReplCompleter

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.py").write_text("a")
    (tmp_path / "src" / "two.py").write_text("b")
    c = ReplCompleter(_ctx(cwd=tmp_path))
    suggestions = c.complete("@src/", 5)
    assert "@src/one.py" in suggestions
    assert "@src/two.py" in suggestions


def test_at_completes_directories_with_trailing_slash(tmp_path: Path) -> None:
    """Directories show up with a trailing '/' so users can keep tabbing in."""
    from anthill.cli.completion import ReplCompleter

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    c = ReplCompleter(_ctx(cwd=tmp_path))
    suggestions = c.complete("@", 1)
    assert "@src/" in suggestions
    assert "@tests/" in suggestions


def test_at_hides_dotfiles(tmp_path: Path) -> None:
    from anthill.cli.completion import ReplCompleter

    (tmp_path / ".hidden").write_text("a")
    (tmp_path / "visible.py").write_text("b")
    c = ReplCompleter(_ctx(cwd=tmp_path))
    suggestions = c.complete("@", 1)
    assert "@.hidden" not in suggestions
    assert "@visible.py" in suggestions


def test_at_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx(cwd=tmp_path))
    assert c.complete("@no/such/dir/x", 14) == []


# --- Non-completion cases ------------------------------------------------


def test_plain_text_no_completion() -> None:
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx(models=("a", "b")))
    assert c.complete("translate this please", 21) == []


def test_cursor_mid_token_uses_text_before_cursor() -> None:
    """Tab in the middle of `/he|llp` completes based on `/he` only."""
    from anthill.cli.completion import ReplCompleter

    c = ReplCompleter(_ctx())
    # cursor=3 → buffer prefix is "/he"; "/help" matches.
    suggestions = c.complete("/hellp", 3)
    assert "/help" in suggestions
