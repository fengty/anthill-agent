"""0.1.42 — tests for the skill-first lookup + post-success distillation.

Covers:
  - ``find_matching_skill``: positive matches, short-request short-circuit,
    threshold filtering, empty-recipes edge cases, the matched_via field.
  - ``suggest_distillation``: slug shape, URL/ID/date templatization.

Why these tests: this module is the linchpin of the citizens-serve-the-king
arc — when a saved recipe fits, we MUST find it before re-planning; when a
hard task completes after refusal-retry, we MUST offer to save it. Both
behaviors compose with the rest of ``nation.ask`` and the REPL UI, so any
silent breakage here turns the new feature into dead code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.recipes import Recipe, RecipeSubtask, save_recipe
from anthill.core.skill_match import (
    MATCH_CONFIDENCE_THRESHOLD,
    MIN_REQUEST_TOKENS,
    DistillationSuggestion,
    SkillMatch,
    find_matching_skill,
    suggest_distillation,
)


def _make_recipe(
    name: str,
    *,
    template: str = "",
    description: str = "",
    subtasks: list[RecipeSubtask] | None = None,
) -> Recipe:
    return Recipe(
        name=name,
        template=template or name,
        description=description,
        subtasks=subtasks or [],
    )


# --- find_matching_skill ----------------------------------------------------


def test_find_matching_skill_returns_none_when_no_recipes(tmp_path: Path) -> None:
    # An empty nation directory must be a clean miss, not a crash.
    assert find_matching_skill("analyze zentao bug 12345 in detail", tmp_path) is None


def test_find_matching_skill_short_request_short_circuits(tmp_path: Path) -> None:
    # MIN_REQUEST_TOKENS guards against coincidental matches on tiny inputs.
    save_recipe(_make_recipe("hello-world", template="hello world"), tmp_path)
    # Two tokens — below the floor regardless of similarity.
    assert MIN_REQUEST_TOKENS >= 3
    assert find_matching_skill("hi there", tmp_path) is None


def test_find_matching_skill_picks_best_above_threshold(tmp_path: Path) -> None:
    # Two recipes; the request should pick the one whose template tokens
    # overlap most with it.
    save_recipe(
        _make_recipe(
            "translate-document",
            template="translate document to French",
            description="run a translation",
        ),
        tmp_path,
    )
    save_recipe(
        _make_recipe(
            "analyze-zentao-bug",
            template="analyze zentao bug ticket and summarize",
            description="dig into a zentao bug ticket and explain the root cause",
            subtasks=[
                RecipeSubtask(task_type="research", prompt_template="fetch zentao bug {id}"),
                RecipeSubtask(task_type="analyze", prompt_template="explain root cause"),
            ],
        ),
        tmp_path,
    )
    match = find_matching_skill(
        "please analyze zentao bug ticket 67890 root cause",
        tmp_path,
    )
    assert match is not None
    assert match.recipe.name == "analyze-zentao-bug"
    assert match.confidence >= MATCH_CONFIDENCE_THRESHOLD
    assert match.matched_via in {"name", "description", "template"}


def test_find_matching_skill_below_threshold_returns_none(tmp_path: Path) -> None:
    # Wildly different topics shouldn't match no matter what.
    save_recipe(
        _make_recipe(
            "translate-document",
            template="translate document to French",
            description="run a translation between two natural languages",
        ),
        tmp_path,
    )
    # Note: token overlap will be near zero with this kitchen-themed request.
    assert (
        find_matching_skill(
            "scrape weather forecast for tomorrow in Beijing",
            tmp_path,
        )
        is None
    )


def test_find_matching_skill_threshold_is_tunable(tmp_path: Path) -> None:
    # Force a very low threshold and confirm a borderline match comes back.
    save_recipe(
        _make_recipe(
            "translate-document",
            template="translate document to French language",
        ),
        tmp_path,
    )
    # One overlapping token ("document") out of many — confidence is low
    # but non-zero. With threshold=0.1 we expect a hit.
    match = find_matching_skill(
        "review my project document carefully please",
        tmp_path,
        threshold=0.1,
    )
    assert match is not None
    assert 0 < match.confidence < MATCH_CONFIDENCE_THRESHOLD


def test_find_matching_skill_returns_skillmatch_shape(tmp_path: Path) -> None:
    save_recipe(
        _make_recipe(
            "analyze-zentao-bug",
            template="analyze zentao bug ticket and summarize root cause",
        ),
        tmp_path,
    )
    match = find_matching_skill(
        "analyze zentao bug ticket and summarize root cause please",
        tmp_path,
    )
    assert isinstance(match, SkillMatch)
    assert isinstance(match.recipe, Recipe)
    assert 0.0 <= match.confidence <= 1.0


def test_find_matching_skill_chinese_tokens(tmp_path: Path) -> None:
    # Chinese characters are tokenized one-per-codepoint so a request and
    # recipe sharing enough hanzi should match. This is what makes the
    # feature useful for the Chinese-speaking users it was designed for.
    # Recipe name is ASCII-only (sanitize_name strips hanzi); the
    # tokens that matter live in template/description.
    save_recipe(
        _make_recipe(
            "zentao-analyze",
            template="分析 禅道 bug 工单 并 总结 根本 原因",
            description="深入 分析 禅道 bug 找到 根本 原因",
        ),
        tmp_path,
    )
    match = find_matching_skill(
        "请 分析 这个 禅道 bug 工单 找到 根本 原因",
        tmp_path,
        threshold=0.3,
    )
    assert match is not None
    assert match.recipe.name == "zentao-analyze"


# --- suggest_distillation ---------------------------------------------------


def test_suggest_distillation_returns_shape() -> None:
    sug = suggest_distillation(
        "analyze zentao bug 56128 root cause",
        ["research", "analyze", "summarize"],
    )
    assert isinstance(sug, DistillationSuggestion)
    assert sug.subtask_signature == ["research", "analyze", "summarize"]


def test_suggest_distillation_slug_is_snake_case_with_dashes() -> None:
    sug = suggest_distillation("Analyze the Zentao bug for me", [])
    # Filler words ("the", "for") dropped; result is hyphen-joined lowercase.
    assert sug.suggested_name == "analyze-zentao-bug-me"


def test_suggest_distillation_url_replaced_with_placeholder() -> None:
    sug = suggest_distillation(
        "analyze https://example.com/zentao/bug-56128.html for root cause",
        ["research"],
    )
    assert "{url}" in sug.template_seed
    assert "https://" not in sug.template_seed


def test_suggest_distillation_numeric_id_replaced_with_placeholder() -> None:
    sug = suggest_distillation("look at bug 56128 right now", ["research"])
    assert "{id}" in sug.template_seed
    assert "56128" not in sug.template_seed


def test_suggest_distillation_date_replaced_with_placeholder() -> None:
    sug = suggest_distillation(
        "summarize the standup notes from 2026-05-17 please",
        ["analyze"],
    )
    assert "{date}" in sug.template_seed
    assert "2026-05-17" not in sug.template_seed


def test_suggest_distillation_empty_request_falls_back() -> None:
    # Pathological empty input must not crash; we want SOME suggested name.
    sug = suggest_distillation("", [])
    assert sug.suggested_name  # non-empty string
    assert sug.subtask_signature == []


def test_suggest_distillation_description_defaults_to_truncated_request() -> None:
    long_request = "a" * 200
    sug = suggest_distillation(long_request, [])
    assert len(sug.description) <= 120


def test_suggest_distillation_description_uses_provided_short_description() -> None:
    sug = suggest_distillation(
        "analyze bug 12345",
        ["research"],
        short_description="zentao-bug-deep-dive",
    )
    assert sug.description == "zentao-bug-deep-dive"


# --- regression guards ------------------------------------------------------


def test_find_matching_skill_tolerates_corrupted_recipe_file(tmp_path: Path) -> None:
    # A garbled TOML file in recipes/ must not blow up the lookup.
    save_recipe(
        _make_recipe(
            "analyze-zentao-bug",
            template="analyze zentao bug ticket root cause",
        ),
        tmp_path,
    )
    (tmp_path / "recipes" / "broken.toml").write_text("not = valid = toml = at = all")
    # Lookup still works on the good recipe.
    match = find_matching_skill(
        "analyze zentao bug ticket and find the root cause",
        tmp_path,
    )
    assert match is not None
    assert match.recipe.name == "analyze-zentao-bug"


@pytest.mark.parametrize(
    "request_text",
    [
        "",
        "hi",            # 1 token
        "hi there",      # 2 tokens
    ],
)
def test_find_matching_skill_short_inputs_all_return_none(
    tmp_path: Path, request_text: str
) -> None:
    save_recipe(
        _make_recipe(
            "analyze-zentao-bug",
            template="analyze zentao bug ticket root cause",
        ),
        tmp_path,
    )
    assert find_matching_skill(request_text, tmp_path) is None
