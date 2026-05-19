"""0.1.65 — skill self-improvement loop tests.

The differentiation move vs hermes: hermes README claims auto-improve
but ships only archival. This test file verifies anthill actually
does the rewrite + tracks drift signal + persists across sessions.

Tests cover:
  - record_quality_signal: first score sets baseline, rolling window
    cap, clamping, defensive on bad types
  - assess_drift: under-min-runs returns no-trigger, drift threshold
  - refine_template: prompt construction, refine_fn called, empty
    output rejected, exception → None
  - apply_refinement: bumps revision, resets history, doesn't touch disk
  - Recipe TOML round-trip: new fields persist + missing fields load
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.recipes import (
    Recipe,
    RecipeSubtask,
    list_recipes,
    save_recipe,
)
from anthill.core.skill_refinement import (
    MIN_DRIFT_FOR_REFINE,
    MIN_RUNS_FOR_REFINE,
    QUALITY_WINDOW_SIZE,
    apply_refinement,
    assess_drift,
    record_quality_signal,
    refine_template,
)


def _fresh_recipe(name: str = "test-skill") -> Recipe:
    return Recipe(
        name=name,
        template="analyze {topic}",
        description="d",
        subtasks=[RecipeSubtask(task_type="general", prompt_template="x")],
    )


# --- record_quality_signal ----------------------------------------------


def test_record_first_score_sets_baseline() -> None:
    r = _fresh_recipe()
    assert r.baseline_quality is None
    record_quality_signal(r, 0.9)
    assert r.baseline_quality == 0.9
    assert r.recent_quality_scores == [0.9]


def test_record_subsequent_scores_dont_change_baseline() -> None:
    r = _fresh_recipe()
    record_quality_signal(r, 0.9)
    record_quality_signal(r, 0.5)
    record_quality_signal(r, 0.4)
    assert r.baseline_quality == 0.9
    assert r.recent_quality_scores == [0.9, 0.5, 0.4]


def test_record_caps_window_at_size() -> None:
    r = _fresh_recipe()
    for s in [0.9, 0.85, 0.8, 0.7, 0.6, 0.5, 0.4]:
        record_quality_signal(r, s)
    # 7 scores in, window=5 → keep last 5.
    assert len(r.recent_quality_scores) == QUALITY_WINDOW_SIZE
    assert r.recent_quality_scores == [0.8, 0.7, 0.6, 0.5, 0.4]


def test_record_clamps_out_of_range() -> None:
    r = _fresh_recipe()
    record_quality_signal(r, 1.5)
    record_quality_signal(r, -0.3)
    assert r.recent_quality_scores == [1.0, 0.0]


def test_record_ignores_non_numeric() -> None:
    r = _fresh_recipe()
    record_quality_signal(r, "high")  # type: ignore[arg-type]
    record_quality_signal(r, None)    # type: ignore[arg-type]
    assert r.recent_quality_scores == []


# --- assess_drift -------------------------------------------------------


def test_assess_drift_no_baseline_returns_none() -> None:
    r = _fresh_recipe()
    assert assess_drift(r) is None


def test_assess_drift_single_sample_returns_none() -> None:
    """Need 2+ samples for a meaningful recent_mean."""
    r = _fresh_recipe()
    record_quality_signal(r, 0.9)
    assert assess_drift(r) is None


def test_assess_drift_below_min_runs_doesnt_flag() -> None:
    r = _fresh_recipe()
    r.run_count = 2  # below MIN_RUNS_FOR_REFINE
    for s in [0.9, 0.5, 0.4]:
        record_quality_signal(r, s)
    report = assess_drift(r)
    assert report is not None
    assert report.needs_refinement is False
    assert report.drift > MIN_DRIFT_FOR_REFINE  # math says yes, but…


def test_assess_drift_flags_when_drift_exceeds_threshold() -> None:
    r = _fresh_recipe()
    r.run_count = MIN_RUNS_FOR_REFINE
    record_quality_signal(r, 0.9)  # baseline
    record_quality_signal(r, 0.5)
    record_quality_signal(r, 0.5)
    record_quality_signal(r, 0.6)
    report = assess_drift(r)
    assert report is not None
    assert report.baseline == 0.9
    # recent_mean = (0.9 + 0.5 + 0.5 + 0.6) / 4 = 0.625
    assert abs(report.recent_mean - 0.625) < 0.001
    # drift = 0.9 - 0.625 = 0.275
    assert report.drift > MIN_DRIFT_FOR_REFINE
    assert report.needs_refinement is True


def test_assess_drift_no_flag_when_quality_stable() -> None:
    r = _fresh_recipe()
    r.run_count = 10
    for s in [0.85, 0.82, 0.86, 0.84]:
        record_quality_signal(r, s)
    report = assess_drift(r)
    assert report is not None
    # drift = 0.85 - 0.8425 = ~0.008 < MIN_DRIFT_FOR_REFINE
    assert report.needs_refinement is False


# --- refine_template ----------------------------------------------------


@pytest.mark.asyncio
async def test_refine_template_calls_refine_fn_with_prompt() -> None:
    r = _fresh_recipe()
    r.run_count = MIN_RUNS_FOR_REFINE
    for s in [0.9, 0.5, 0.5]:
        record_quality_signal(r, s)

    captured: dict = {}

    async def fake_refine(prompt: str) -> str:
        captured["prompt"] = prompt
        return "analyze {topic} thoroughly with examples"

    new_text = await refine_template(
        r,
        recent_request="analyze python decorators",
        recent_output="A decorator is a function that wraps...",
        refine_fn=fake_refine,
    )
    assert new_text == "analyze {topic} thoroughly with examples"
    # Prompt mentions the drift numbers and the current template.
    assert "baseline" in captured["prompt"]
    assert "analyze {topic}" in captured["prompt"]
    assert "{topic}" in captured["prompt"]  # placeholders preserved hint


@pytest.mark.asyncio
async def test_refine_template_returns_none_when_no_drift() -> None:
    """Don't refine when there's no measurable drift."""
    r = _fresh_recipe()
    # Only one sample → assess_drift returns None.
    record_quality_signal(r, 0.9)

    async def fake_refine(prompt: str) -> str:
        return "should not be called"

    new_text = await refine_template(
        r,
        recent_request="x",
        recent_output="y",
        refine_fn=fake_refine,
    )
    assert new_text is None


@pytest.mark.asyncio
async def test_refine_template_empty_output_returns_none() -> None:
    r = _fresh_recipe()
    r.run_count = MIN_RUNS_FOR_REFINE
    for s in [0.9, 0.5, 0.5]:
        record_quality_signal(r, s)

    async def fake_refine(_):
        return "   "

    assert await refine_template(
        r, recent_request="x", recent_output="y", refine_fn=fake_refine
    ) is None


@pytest.mark.asyncio
async def test_refine_template_exception_returns_none() -> None:
    """A failing model call must not propagate."""
    r = _fresh_recipe()
    r.run_count = MIN_RUNS_FOR_REFINE
    for s in [0.9, 0.5, 0.5]:
        record_quality_signal(r, s)

    async def boom(_):
        raise RuntimeError("model down")

    assert await refine_template(
        r, recent_request="x", recent_output="y", refine_fn=boom
    ) is None


# --- apply_refinement ---------------------------------------------------


def test_apply_refinement_bumps_revision_and_resets_history() -> None:
    r = _fresh_recipe()
    r.run_count = MIN_RUNS_FOR_REFINE
    for s in [0.9, 0.5, 0.5]:
        record_quality_signal(r, s)
    assert r.template_revisions == 0

    apply_refinement(r, "new improved {topic} template")
    assert r.template == "new improved {topic} template"
    assert r.template_revisions == 1
    # History reset so the next 5 runs set a new baseline.
    assert r.recent_quality_scores == []
    assert r.baseline_quality is None


def test_apply_refinement_repeated_bumps_revision() -> None:
    r = _fresh_recipe()
    apply_refinement(r, "v2")
    apply_refinement(r, "v3")
    apply_refinement(r, "v4")
    assert r.template_revisions == 3
    assert r.template == "v4"


# --- Recipe TOML persistence (the new quality fields survive disk) -------


def test_quality_fields_round_trip_on_disk(tmp_path: Path) -> None:
    r = _fresh_recipe("my-skill")
    r.run_count = 5
    r.baseline_quality = 0.9
    r.recent_quality_scores = [0.9, 0.5, 0.6, 0.55, 0.5]
    r.template_revisions = 2
    save_recipe(r, tmp_path)

    [reloaded] = list_recipes(tmp_path)
    assert reloaded.baseline_quality == 0.9
    assert reloaded.recent_quality_scores == [0.9, 0.5, 0.6, 0.55, 0.5]
    assert reloaded.template_revisions == 2


def test_legacy_recipe_without_quality_fields_loads_clean(
    tmp_path: Path,
) -> None:
    """Pre-0.1.65 recipes have no quality fields. They must load with
    defaults (None baseline, empty scores list, 0 revisions) — older
    user files MUST still work after the upgrade."""
    (tmp_path / "recipes").mkdir()
    (tmp_path / "recipes" / "legacy.toml").write_text(
        'name = "legacy"\n'
        'template = "hello {topic}"\n'
        "created_at = 1700000000.0\n"
        "run_count = 0\n"
        "\n[[subtasks]]\n"
        'task_type = "general"\n'
        'prompt_template = "x"\n'
        "depends_on = []\n"
    )
    [r] = list_recipes(tmp_path)
    assert r.baseline_quality is None
    assert r.recent_quality_scores == []
    assert r.template_revisions == 0
