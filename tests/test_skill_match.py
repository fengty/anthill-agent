"""0.1.42 — tests for skill-first lookup + post-success distillation.

Focused on behaviors that would actually break the user-facing arc:
  - matching the right skill when one fits (recipe is reused)
  - rejecting weak matches (no false positives)
  - distillation produces a runnable template with placeholders for
    variable parts (URL / id / date) so the recipe is reusable
  - worth-saving signals identify ASKS that earned their save

The original suite (0.2.21 reset) had ~60 tests covering each rule
in `worth_saving` and `extract_variables` separately. Most were
"verify constant X" or "one example per regex group" — easy to
collapse without losing coverage.
"""

from __future__ import annotations

from pathlib import Path

from anthill.core.recipes import Recipe, RecipeSubtask, save_recipe
from anthill.core.skill_match import (
    MIN_REQUEST_TOKENS,
    distill_request_to_recipe_fields,
    extract_variables,
    find_matching_skill,
    is_trivial_request,
    skill_save_signals,
    suggest_distillation,
    unique_slug,
    worth_saving_as_skill,
)


def _make_recipe(
    name: str,
    *,
    template: str = "",
    subtasks: list[RecipeSubtask] | None = None,
) -> Recipe:
    return Recipe(
        name=name,
        template=template or name,
        description="",
        subtasks=subtasks or [],
    )


# --- find_matching_skill: end-to-end matching contract ---------------


def test_matching_skill_picks_best_above_threshold(tmp_path: Path) -> None:
    """Two recipes, one clearly fits → that one wins. The other is
    semantically distinct enough to fall below the threshold."""
    save_recipe(_make_recipe("translate-doc", template="translate document to French"), tmp_path)
    save_recipe(_make_recipe("zentao-bug", template="analyze zentao bug 12345"), tmp_path)

    m = find_matching_skill("analyze zentao bug 67890 in detail", tmp_path)
    assert m is not None
    assert m.recipe.name == "zentao-bug"
    assert m.confidence >= 0.3
    assert m.matched_via  # any non-empty signal field


def test_no_match_when_request_unrelated(tmp_path: Path) -> None:
    """A request nowhere near any recipe → None, NOT a low-confidence
    false positive."""
    save_recipe(_make_recipe("translate-doc", template="translate document to French"), tmp_path)
    assert find_matching_skill("show me the weather in Tokyo today", tmp_path) is None


def test_no_recipes_returns_none(tmp_path: Path) -> None:
    """Empty dir → clean None, no crash."""
    assert find_matching_skill("analyze zentao bug 12345", tmp_path) is None


def test_too_short_request_short_circuits(tmp_path: Path) -> None:
    """Below MIN_REQUEST_TOKENS, no coincidental match should fire."""
    save_recipe(_make_recipe("hello", template="hello world"), tmp_path)
    assert find_matching_skill("hi", tmp_path) is None
    assert MIN_REQUEST_TOKENS >= 3


def test_matching_skill_corrupt_recipe_file_doesnt_crash(tmp_path: Path) -> None:
    """One bad recipe doesn't crash the lookup. Whether the good
    one matches a specific request is a separate concern; the
    contract here is just 'don't raise.'"""
    save_recipe(_make_recipe("good", template="ping host 1.2.3.4"), tmp_path)
    (tmp_path / "recipes" / "broken.toml").write_text("garbage [", encoding="utf-8")
    # Just shouldn't raise — None or a SkillMatch are both fine.
    find_matching_skill("anything here", tmp_path)


# --- extract_variables / distillation -------------------------------


def test_extract_variables_finds_url_id_and_date() -> None:
    """The three placeholder kinds we templatize: URL, numeric ID,
    ISO date. One test covers all three because the impl uses one
    pass over the same regex set."""
    text = "fetch https://zentao.com/bug-12345 on 2026-05-24"
    vars_ = extract_variables(text)
    # Each kind appears.
    assert any(v.startswith("url") for v in vars_)
    assert any(v.startswith("id") for v in vars_)
    assert any(v.startswith("date") for v in vars_)


def test_suggest_distillation_replaces_variables_with_placeholders() -> None:
    """The slug stays English-friendly; URL/id/date in the
    template_seed become `{url}` / `{id}` / `{date}` so the skill
    is reusable."""
    suggestion = suggest_distillation(
        "analyze zentao bug 12345 from https://x.com on 2026-05-24",
        plan_task_types=["research", "analyze"],
    )
    seed = suggestion.template_seed
    assert "{url}" in seed or "{id}" in seed or "{date}" in seed
    # Slug is snake-case-ish (no spaces / colons).
    assert " " not in suggestion.suggested_name
    assert ":" not in suggestion.suggested_name


# --- worth_saving heuristic -----------------------------------------


def test_worth_saving_rejects_trivial() -> None:
    """A greeting / pleasantry is never a skill worth saving."""
    assert is_trivial_request("你好")
    should_save, _reason = worth_saving_as_skill(request="你好")
    assert not should_save


def test_worth_saving_accepts_refusal_retry_arc() -> None:
    """The canonical 'earned save': a hard ask that needed refusal-
    retry to land + a plan with at least 2 subtasks. Refusal-retry
    is one of the high-weight signals; with subtask diversity it
    crosses the SAVE_SCORE_THRESHOLD."""
    class _ST:
        def __init__(self, tt): self.task_type = tt
    should_save, reason = worth_saving_as_skill(
        request="research https://example.com bug 12345 and synthesize a plan",
        plan_subtasks=[_ST("research"), _ST("synthesize")],
        had_refusal_retry=True,
        final_output="A long final answer with structure and substance " * 5,
    )
    assert should_save, f"expected save; got reason: {reason}"


def test_worth_saving_rejects_single_subtask() -> None:
    """Hard reject: a one-subtask plan isn't a workflow worth a
    reusable recipe."""
    class _ST:
        def __init__(self, tt): self.task_type = tt
    should_save, _ = worth_saving_as_skill(
        request="X X X X X X X X X X X X X",  # not trivial
        plan_subtasks=[_ST("research")],
        had_refusal_retry=True,
    )
    assert not should_save


def test_skill_save_signals_returns_named_dict() -> None:
    """skill_save_signals returns a dict the caller unpacks. The
    keyset is the contract — caller code reads specific keys."""
    signals = skill_save_signals(
        request="analyze https://example.com bug 12345 in detail please",
        had_refusal_retry=False,
        final_output="X",
    )
    assert isinstance(signals, dict)
    assert "variable_content" in signals


# --- unique_slug ---------------------------------------------------


def test_unique_slug_adds_suffix_on_collision() -> None:
    """Same name twice → second gets a numeric suffix, no crash."""
    base = unique_slug("translate-doc", {"translate-doc"})
    assert base != "translate-doc"
    assert "translate-doc" in base


# --- distill_request_to_recipe_fields end-to-end ------------------


def test_distill_returns_template_description_subtasks() -> None:
    """The full distillation flow: from request + subtasks → recipe
    tuple ready to save. Verifies the FORMAT, not specific wording."""
    slug, template, description, sub_tuples = distill_request_to_recipe_fields(
        "analyze zentao bug 12345 in detail",
        [
            ("research", "look up the bug", []),
            ("analyze", "explain the root cause", ["research"]),
        ],
        set(),
    )
    assert slug and isinstance(slug, str)
    assert template and isinstance(template, str)
    assert isinstance(sub_tuples, list)
    assert len(sub_tuples) == 2
    # Each subtask tuple has 3 items (task_type, prompt_template, deps).
    for tt, pt, deps in sub_tuples:
        assert isinstance(tt, str)
        assert isinstance(pt, str)
        assert isinstance(deps, list)
