"""0.1.65 — skill self-improvement loop.

Hermes README claims "skills auto-improve in use" but its code only
archives stale skills — no actual rewrite path. anthill builds the
real version:

  1. **Sample quality** every time a skill matches and runs. Record
     the LATEST quality signal in a rolling window on the recipe.
  2. **Detect drift**: when recent quality dips materially below
     the baseline (set when the skill was first saved), flag the
     skill for refinement.
  3. **Refine**: use the model to produce an updated template based
     on the most recent successful instance + the drift signal.
     Bump `template_revisions`; the old template is recoverable
     via git (recipes are TOML on disk).

Refinement is gated by:
  - `run_count >= 3` so we have enough samples to trust the baseline
  - drift `>= MIN_DRIFT_FOR_REFINE` (0.15) — otherwise it's noise
  - user opt-in via `/skill refine X` (we propose, user commits)

Why opt-in: auto-rewriting a saved recipe is destructive. Better to
nudge the user with "this skill's quality has dropped — refine?"
than silently mutate the template. The user can still re-run
refinement repeatedly without manual edits (cheap), and they keep
veto power.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from anthill.core.recipes import Recipe


# Rolling window size for recent quality. Tighter window = faster
# detection of drift, but more variance noise. 5 is the smallest
# window where mean is meaningful for skill use (1-2 outliers
# wouldn't dominate).
QUALITY_WINDOW_SIZE: int = 5

# Minimum run_count before refinement is considered. Below 3 we
# don't have a statistical baseline yet — the first 1-2 uses might
# vary just from input variance, not template quality.
MIN_RUNS_FOR_REFINE: int = 3

# Minimum drift (baseline - recent_mean) that triggers a refinement
# proposal. 0.15 ≈ "went from 0.85 to 0.70" — noticeable to the
# user, not noise.
MIN_DRIFT_FOR_REFINE: float = 0.15


def record_quality_signal(
    recipe: Recipe, score: float
) -> None:
    """Add a fresh quality score to the rolling window.

    Side effect: when this is the FIRST score and there's no
    baseline yet, the score becomes the baseline. Subsequent scores
    just append to recent_quality_scores (which gets capped at
    QUALITY_WINDOW_SIZE).

    Score range: [0.0, 1.0]. Out-of-range is clamped — defensive
    against judges that return raw logprobs or other unbounded values.
    """
    if not isinstance(score, (int, float)):
        return
    s = max(0.0, min(1.0, float(score)))
    recipe.recent_quality_scores.append(s)
    # Cap rolling window. Dropping from the front (FIFO) keeps the
    # MOST recent N samples — which is exactly what drift detection
    # needs.
    if len(recipe.recent_quality_scores) > QUALITY_WINDOW_SIZE:
        recipe.recent_quality_scores = recipe.recent_quality_scores[
            -QUALITY_WINDOW_SIZE:
        ]
    if recipe.baseline_quality is None:
        recipe.baseline_quality = s


@dataclass(frozen=True)
class DriftReport:
    """Diagnostic snapshot of one recipe's quality trajectory."""

    baseline: float
    recent_mean: float
    drift: float          # baseline - recent_mean (positive = degrading)
    sample_size: int
    needs_refinement: bool


def assess_drift(recipe: Recipe) -> DriftReport | None:
    """Compute drift vs baseline. Returns None when we don't have
    enough data to assess (no baseline / fewer than 2 samples)."""
    if recipe.baseline_quality is None:
        return None
    scores = recipe.recent_quality_scores
    if len(scores) < 2:
        return None
    recent_mean = sum(scores) / len(scores)
    drift = recipe.baseline_quality - recent_mean
    needs = (
        recipe.run_count >= MIN_RUNS_FOR_REFINE
        and drift >= MIN_DRIFT_FOR_REFINE
    )
    return DriftReport(
        baseline=recipe.baseline_quality,
        recent_mean=recent_mean,
        drift=drift,
        sample_size=len(scores),
        needs_refinement=needs,
    )


# Type alias for caller-supplied LLM call. Async because the model
# call is I/O-bound; the caller (REPL or daemon) already lives in
# an asyncio context.
RefineFn = Callable[[str], Awaitable[str]]


REFINE_PROMPT_TEMPLATE = """You are refining a saved workflow template (called a "skill").
The skill has been used {run_count} times. Its quality has degraded:

  baseline_quality (first use): {baseline:.2f}
  recent_mean (last {n_recent} uses): {recent_mean:.2f}
  drift: -{drift:.2f}

CURRENT TEMPLATE:
{template}

MOST RECENT SUCCESSFUL INSTANCE OF USING THIS SKILL:
  request: {recent_request}
  output (first 600 chars):
{recent_output_snippet}

YOUR TASK: produce an updated template that would have worked better
for cases like the recent one. Keep all placeholders ({placeholders})
intact. Keep the template GENERAL (don't bake in this one instance's
specifics — the {{id}}/{{url}}/{{date}} placeholders are for that).

OUTPUT FORMAT: just the new template text, no explanation, no quotes.
"""


async def refine_template(
    recipe: Recipe,
    *,
    recent_request: str,
    recent_output: str,
    refine_fn: RefineFn,
) -> str | None:
    """Ask the model for a better template. Returns the new text or
    None on failure / empty output. The caller decides whether to
    save it back to disk.
    """
    drift = assess_drift(recipe)
    if drift is None:
        return None
    placeholders = ", ".join("{" + p + "}" for p in recipe.placeholders())
    prompt = REFINE_PROMPT_TEMPLATE.format(
        run_count=recipe.run_count,
        baseline=drift.baseline,
        n_recent=drift.sample_size,
        recent_mean=drift.recent_mean,
        drift=drift.drift,
        template=recipe.template,
        recent_request=recent_request[:200],
        recent_output_snippet=recent_output[:600],
        placeholders=placeholders or "(none)",
    )
    try:
        new_text = await refine_fn(prompt)
    except Exception:  # noqa: BLE001 — refinement failure is not fatal
        return None
    if not new_text or not new_text.strip():
        return None
    return new_text.strip()


def apply_refinement(recipe: Recipe, new_template: str) -> None:
    """Commit the refined template in place.

    - Bumps `template_revisions`.
    - Resets `recent_quality_scores` so the next 5 uses become the
      new baseline rather than dragging the old degraded average.
    - Resets `baseline_quality` to None; first new quality signal
      will set it (same as initial save).

    The caller still has to save_recipe() — we don't touch disk
    here, keeping side-effect locality.
    """
    recipe.template = new_template
    recipe.template_revisions += 1
    recipe.recent_quality_scores = []
    recipe.baseline_quality = None
