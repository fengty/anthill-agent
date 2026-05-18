"""0.1.50 — usage stats + staleness for saved skills.

Built on top of 0.1.49 (which finally started writing run_count and
last_run_at on every skill match). This module owns the *reading*
side: render usage stats for /skill list, sort skills so the
workhorses surface, and decide what counts as "stale" for the
0.1.51 prune flow.

Pure functions. No I/O. Caller passes Recipe objects; we don't
touch disk. That keeps the rendering testable without fixtures.
"""

from __future__ import annotations

import time
from typing import Iterable

from anthill.core.recipes import Recipe


# A skill is "stale" when it was created more than `STALE_DAYS` days
# ago AND has never been matched. The "never matched" guard is what
# distinguishes stale (dead weight) from "just-created and hasn't
# had a chance yet". 14 days is two weeks — long enough for any
# realistic recurring workflow to come back around at least once.
STALE_DAYS: int = 14


def is_stale(recipe: Recipe, *, now: float | None = None) -> bool:
    """Has this recipe been on disk for STALE_DAYS+ days with no matches?

    Defensive on missing timestamps: a recipe with `created_at=None`
    or run_count=0 from before 0.1.49 still loads but we conservatively
    treat it as "freshly created" rather than stale, so we don't
    accidentally prune a user's pre-tracking skills.
    """
    if recipe.run_count > 0:
        return False
    now = now if now is not None else time.time()
    created = recipe.created_at or now
    age_days = (now - created) / 86400.0
    return age_days >= STALE_DAYS


def _humanize_age(seconds: float) -> str:
    """seconds since some past time → 'just now' / '3m ago' / '2h ago' / '5d ago'.

    Bias toward coarse units past 1 hour — exact-minute timestamps
    are noise in a /skill list overview.
    """
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def format_skill_stats(recipe: Recipe, *, now: float | None = None) -> str:
    """Render the usage chip shown in /skill list. Examples:

      used 12 times, last 2h ago
      used 1 time, last 5d ago
      never used — 21d old (🌫 stale)
      never used — just saved

    `now` is overridable so tests don't depend on the wall clock.
    """
    now = now if now is not None else time.time()
    if recipe.run_count == 0:
        created = recipe.created_at or now
        age_days = (now - created) / 86400.0
        if is_stale(recipe, now=now):
            return f"never used — {int(age_days)}d old (🌫 stale)"
        if age_days >= 1:
            return f"never used — {int(age_days)}d old"
        return "never used — just saved"

    plural = "times" if recipe.run_count != 1 else "time"
    if recipe.last_run_at is None:
        return f"used {recipe.run_count} {plural}"
    delta = max(0.0, now - recipe.last_run_at)
    return f"used {recipe.run_count} {plural}, last {_humanize_age(delta)}"


def sort_recipes_by_usage(recipes: Iterable[Recipe]) -> list[Recipe]:
    """Sort for display: most-used first, then most-recent, then name.

    Why this order: the user's *workhorse skills* should be the top
    of /skill list. Stale or never-used skills sink to the bottom
    where they're visually obvious candidates for /skill prune.

    Ties broken by `last_run_at` (more-recent first) so among equally-
    used skills, the freshly-active one wins the top slot.
    """
    return sorted(
        recipes,
        key=lambda r: (
            -r.run_count,
            -(r.last_run_at or 0.0),
            r.name,
        ),
    )


def partition_stale(
    recipes: Iterable[Recipe], *, now: float | None = None
) -> tuple[list[Recipe], list[Recipe]]:
    """Split into (stale, keep) lists for use by /skill prune.

    Stable order within each partition so the prune confirmation
    shows the same list as /skill list does.
    """
    now = now if now is not None else time.time()
    stale: list[Recipe] = []
    keep: list[Recipe] = []
    for r in recipes:
        (stale if is_stale(r, now=now) else keep).append(r)
    return stale, keep
