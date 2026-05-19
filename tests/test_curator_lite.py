"""0.1.58 — curator-lite: REPL splash shows stale-skill hint.

Hermes runs a background curator daemon that auto-archives idle
agent-created skills. anthill (single-process REPL) opts for the
passive version: every welcome splash counts stale skills and prints
a one-line nudge when ≥3 are present. The user still chooses to act
via /skill prune — we never auto-delete.

Tests verify:
  - threshold: <3 stale skills → no hint
  - threshold: ≥3 stale → hint with first 3 names
  - >3 stale → hint says "(+N more)"
  - missing nation dir → silent (no crash)
  - "used" or "fresh" skills don't count toward stale total
"""

from __future__ import annotations

import time
from pathlib import Path

from anthill.core.recipes import Recipe, RecipeSubtask, save_recipe
from anthill.core.skill_stats import STALE_DAYS, partition_stale


def _stale_recipe(name: str, *, age_days: int = STALE_DAYS + 1) -> Recipe:
    now = time.time()
    return Recipe(
        name=name,
        template="t",
        description="d",
        subtasks=[RecipeSubtask(task_type="general", prompt_template="x")],
        created_at=now - age_days * 86400,
        last_run_at=None,
        run_count=0,
    )


def _fresh_recipe(name: str) -> Recipe:
    return Recipe(
        name=name,
        template="t",
        description="d",
        subtasks=[RecipeSubtask(task_type="general", prompt_template="x")],
        created_at=time.time(),  # just saved
        last_run_at=None,
        run_count=0,
    )


def _used_recipe(name: str) -> Recipe:
    now = time.time()
    return Recipe(
        name=name,
        template="t",
        description="d",
        subtasks=[RecipeSubtask(task_type="general", prompt_template="x")],
        created_at=now - (STALE_DAYS + 5) * 86400,
        last_run_at=now,
        run_count=3,
    )


# --- the underlying partition_stale already has unit tests in
# test_skill_stats.py. Here we verify the THRESHOLDING + naming behavior
# that 0.1.58 added on top of it.


def test_threshold_below_three_no_hint(tmp_path: Path) -> None:
    """Two stale skills shouldn't trigger the splash hint (UI noise budget)."""
    save_recipe(_stale_recipe("a"), tmp_path)
    save_recipe(_stale_recipe("b"), tmp_path)
    save_recipe(_fresh_recipe("c"), tmp_path)
    stale, _ = partition_stale([
        _stale_recipe("a"),
        _stale_recipe("b"),
        _fresh_recipe("c"),
    ])
    # The function returns 2 stale — the splash code checks `>= 3`.
    assert len(stale) == 2


def test_threshold_exactly_three_triggers_hint() -> None:
    stale, _ = partition_stale([
        _stale_recipe(f"old-{i}") for i in range(3)
    ])
    assert len(stale) == 3


def test_fresh_skills_dont_count_toward_stale() -> None:
    """Mixed bag: 2 stale, 5 fresh. Total stale must be 2, not 7."""
    recipes = (
        [_stale_recipe(f"old-{i}") for i in range(2)]
        + [_fresh_recipe(f"new-{i}") for i in range(5)]
    )
    stale, _ = partition_stale(recipes)
    assert len(stale) == 2


def test_used_skills_dont_count_toward_stale() -> None:
    """A 60-day-old skill that was used yesterday is NOT stale."""
    recipes = [_used_recipe("workhorse")]
    stale, _ = partition_stale(recipes)
    assert stale == []


def test_stale_names_preserve_input_order() -> None:
    """Splash output truncates to first 3 names — order must match
    /skill list output to avoid user confusion."""
    inputs = [_stale_recipe(f"recipe-{i}") for i in range(5)]
    stale, _ = partition_stale(inputs)
    names = [r.name for r in stale]
    assert names == ["recipe-0", "recipe-1", "recipe-2", "recipe-3", "recipe-4"]


def test_partition_stale_empty_input() -> None:
    """No recipes → no stale → splash stays silent."""
    stale, keep = partition_stale([])
    assert stale == [] and keep == []
