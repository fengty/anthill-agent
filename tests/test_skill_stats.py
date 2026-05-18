"""0.1.50 — tests for skill usage stats + staleness detection.

Covers:
  - `is_stale`: age threshold, "never used" guard, defensive on missing fields
  - `format_skill_stats`: all phrasings (just saved / stale / used N times)
  - `sort_recipes_by_usage`: workhorses surface, unused sink
  - `partition_stale`: clean split for /skill prune

Pure functions; no I/O — easy to test with synthetic Recipe objects.
"""

from __future__ import annotations

import time

from anthill.core.recipes import Recipe, RecipeSubtask
from anthill.core.skill_stats import (
    STALE_DAYS,
    format_skill_stats,
    is_stale,
    partition_stale,
    sort_recipes_by_usage,
)


def _recipe(
    name: str,
    *,
    run_count: int = 0,
    last_run_at: float | None = None,
    created_at: float | None = None,
) -> Recipe:
    return Recipe(
        name=name,
        template="t",
        description="d",
        subtasks=[RecipeSubtask(task_type="general", prompt_template="x")],
        created_at=created_at if created_at is not None else time.time(),
        last_run_at=last_run_at,
        run_count=run_count,
    )


# --- is_stale -------------------------------------------------------------


def test_is_stale_used_skill_never_stale() -> None:
    # Anything that's been matched at least once is by definition not
    # stale, no matter how old.
    r = _recipe("x", run_count=1, created_at=time.time() - 365 * 86400)
    assert is_stale(r) is False


def test_is_stale_fresh_unused_not_stale() -> None:
    r = _recipe("x", run_count=0, created_at=time.time() - 86400)  # 1 day
    assert is_stale(r) is False


def test_is_stale_old_unused_is_stale() -> None:
    now = time.time()
    r = _recipe("x", run_count=0, created_at=now - (STALE_DAYS + 1) * 86400)
    assert is_stale(r, now=now) is True


def test_is_stale_exact_threshold_boundary() -> None:
    now = time.time()
    r = _recipe("x", run_count=0, created_at=now - STALE_DAYS * 86400)
    # Stale at exactly STALE_DAYS (inclusive boundary). Documenting
    # so future tweaks don't shift the boundary silently.
    assert is_stale(r, now=now) is True


# --- format_skill_stats ---------------------------------------------------


def test_format_used_once_singular() -> None:
    now = time.time()
    r = _recipe("x", run_count=1, last_run_at=now - 60)
    s = format_skill_stats(r, now=now)
    assert "used 1 time" in s
    assert "1 times" not in s  # singular grammar


def test_format_used_many_plural() -> None:
    now = time.time()
    r = _recipe("x", run_count=12, last_run_at=now - 7200)  # 2h
    s = format_skill_stats(r, now=now)
    assert "used 12 times" in s
    assert "2h ago" in s


def test_format_never_used_just_saved() -> None:
    now = time.time()
    r = _recipe("x", run_count=0, created_at=now - 60)
    assert "just saved" in format_skill_stats(r, now=now)


def test_format_never_used_old_but_not_stale() -> None:
    now = time.time()
    r = _recipe("x", run_count=0, created_at=now - 5 * 86400)  # 5 days
    s = format_skill_stats(r, now=now)
    assert "5d old" in s
    assert "stale" not in s.lower()


def test_format_never_used_stale() -> None:
    now = time.time()
    r = _recipe(
        "x", run_count=0, created_at=now - (STALE_DAYS + 7) * 86400
    )  # 21 days
    s = format_skill_stats(r, now=now)
    assert "stale" in s.lower()
    assert "21d" in s


def test_format_age_humanization_brackets() -> None:
    # Spot-check each unit boundary so future _humanize_age tweaks
    # don't accidentally shift them.
    now = time.time()
    for delta, expected_substr in [
        (30.0, "just now"),
        (90.0, "1m ago"),
        (60 * 5, "5m ago"),
        (3600 * 3, "3h ago"),
        (86400 * 2, "2d ago"),
    ]:
        r = _recipe("x", run_count=1, last_run_at=now - delta)
        s = format_skill_stats(r, now=now)
        assert expected_substr in s, (
            f"delta={delta}s expected {expected_substr!r}, got {s!r}"
        )


# --- sort_recipes_by_usage ------------------------------------------------


def test_sort_workhorses_first() -> None:
    now = time.time()
    recipes = [
        _recipe("unused", run_count=0, created_at=now),
        _recipe("workhorse", run_count=10, last_run_at=now),
        _recipe("occasional", run_count=2, last_run_at=now - 3600),
    ]
    ordered = sort_recipes_by_usage(recipes)
    assert [r.name for r in ordered] == ["workhorse", "occasional", "unused"]


def test_sort_tiebreak_by_recency_then_name() -> None:
    now = time.time()
    recipes = [
        _recipe("b-newer", run_count=3, last_run_at=now),
        _recipe("a-older", run_count=3, last_run_at=now - 3600),
        _recipe("c-newest", run_count=3, last_run_at=now + 1),
    ]
    ordered = sort_recipes_by_usage(recipes)
    # Recency wins; for equal recency, alphabetical name.
    assert [r.name for r in ordered] == ["c-newest", "b-newer", "a-older"]


def test_sort_handles_missing_last_run_at() -> None:
    recipes = [
        _recipe("unused-a", run_count=0, last_run_at=None),
        _recipe("unused-b", run_count=0, last_run_at=None),
    ]
    ordered = sort_recipes_by_usage(recipes)
    # No crash; alphabetical fallback.
    assert [r.name for r in ordered] == ["unused-a", "unused-b"]


# --- partition_stale ------------------------------------------------------


def test_partition_stale_separates_correctly() -> None:
    now = time.time()
    recipes = [
        _recipe("keep-used", run_count=1, last_run_at=now),
        _recipe("keep-fresh", run_count=0, created_at=now - 86400),
        _recipe(
            "stale-old", run_count=0, created_at=now - (STALE_DAYS + 1) * 86400
        ),
    ]
    stale, keep = partition_stale(recipes, now=now)
    assert [r.name for r in stale] == ["stale-old"]
    assert {r.name for r in keep} == {"keep-used", "keep-fresh"}


def test_partition_stable_order_within_each() -> None:
    """Partition must preserve input order — so the prune confirmation
    shows the same list as /skill list."""
    now = time.time()
    recipes = [
        _recipe("a", run_count=0, created_at=now - (STALE_DAYS + 1) * 86400),
        _recipe("b", run_count=1, last_run_at=now),
        _recipe("c", run_count=0, created_at=now - (STALE_DAYS + 2) * 86400),
    ]
    stale, keep = partition_stale(recipes, now=now)
    assert [r.name for r in stale] == ["a", "c"]
    assert [r.name for r in keep] == ["b"]
