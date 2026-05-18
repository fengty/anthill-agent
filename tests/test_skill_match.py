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
    distill_request_to_recipe_fields,
    find_matching_skill,
    is_trivial_request,
    suggest_distillation,
    unique_slug,
    worth_saving_as_skill,
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


# --- unique_slug ------------------------------------------------------------


def test_unique_slug_returns_base_when_free() -> None:
    assert unique_slug("analyze-bug", []) == "analyze-bug"
    assert unique_slug("analyze-bug", ["other"]) == "analyze-bug"


def test_unique_slug_suffixes_when_taken() -> None:
    # First collision -> -2, two collisions -> -3, etc. Matches the
    # auto-save loop's expectations in repl.py.
    assert unique_slug("analyze-bug", ["analyze-bug"]) == "analyze-bug-2"
    assert (
        unique_slug("analyze-bug", ["analyze-bug", "analyze-bug-2"])
        == "analyze-bug-3"
    )
    # Sparse holes: if -2 and -3 are taken but -4 is free, we should
    # land on -4 (we never reuse a hole; the suffix is monotonic).
    assert (
        unique_slug(
            "analyze-bug",
            ["analyze-bug", "analyze-bug-2", "analyze-bug-3"],
        )
        == "analyze-bug-4"
    )


# --- distill_request_to_recipe_fields ---------------------------------------


def test_distill_request_to_recipe_fields_basic_shape() -> None:
    slug, template, description, subs = distill_request_to_recipe_fields(
        "analyze zentao bug 56128 root cause please",
        [
            ("research", "fetch zentao bug 56128 details", []),
            ("analyze", "explain the root cause of bug 56128", ["research"]),
        ],
        existing_names=[],
    )
    # Slug is the auto-generated name.
    assert slug
    # Top-level template has {id} where 56128 used to be.
    assert "{id}" in template
    assert "56128" not in template
    # Description is the request truncated.
    assert description.startswith("analyze zentao bug")
    # Subtask prompts also templatized.
    assert len(subs) == 2
    assert all("{id}" in prompt for _, prompt, _ in subs)
    assert all("56128" not in prompt for _, prompt, _ in subs)
    # Task types and deps preserved.
    assert subs[0][0] == "research"
    assert subs[1][0] == "analyze"
    assert subs[1][2] == ["research"]


def test_distill_request_to_recipe_fields_de_duplicates_slug() -> None:
    # When the auto-generated slug already exists, we must get a
    # suffixed one — otherwise back-to-back hard asks overwrite each
    # other and the king loses skills he just earned.
    slug, _, _, _ = distill_request_to_recipe_fields(
        "analyze zentao bug",
        [("research", "do it", [])],
        existing_names=["analyze-zentao-bug"],
    )
    assert slug == "analyze-zentao-bug-2"


def test_distill_request_to_recipe_fields_accepts_iterator() -> None:
    # The function materializes its plan_subtasks input — passing
    # an iterator (single-pass) must still work, not silently produce
    # empty subtasks on the second pass.
    def gen():
        yield ("research", "do thing 1", [])
        yield ("analyze", "do thing 2", ["research"])

    _, _, _, subs = distill_request_to_recipe_fields(
        "test request long enough",
        gen(),
        existing_names=[],
    )
    assert len(subs) == 2


def test_distill_request_to_recipe_fields_empty_plan() -> None:
    # No subtasks (shouldn't fire from REPL, but the helper must not
    # crash). Slug and template still come back.
    slug, template, _, subs = distill_request_to_recipe_fields(
        "analyze bug 56128",
        [],
        existing_names=[],
    )
    assert slug
    assert subs == []
    assert "{id}" in template


# --- is_trivial_request (0.1.45 — judgment for what's NOT a skill) ---------


class _FakeSubtask:
    """Minimal stand-in for scout.Subtask in worth_saving_as_skill tests."""

    def __init__(self, task_type: str) -> None:
        self.task_type = task_type


@pytest.mark.parametrize(
    "request_text",
    [
        "你好",
        "您好",
        "你好！",      # punctuation stripped
        "你好 ",        # trailing space stripped
        "hi",
        "Hi",          # case-insensitive
        "hello",
        "thanks",
        "thank you",
        "ok",
        "好的",
        "再见",
        "test",
        "ping",
        "",             # empty == most trivial
    ],
)
def test_is_trivial_request_matches_pleasantries(request_text: str) -> None:
    assert is_trivial_request(request_text), (
        f"{request_text!r} should be flagged as trivial — it's a pleasantry, "
        f"not a skill candidate"
    )


@pytest.mark.parametrize(
    "request_text",
    [
        "analyze zentao bug 56128 root cause",
        "你好 can you analyze this bug",  # leading hi but real ask underneath
        "translate this document to French",
        "summarize the standup notes from last week",
        "帮我分析下这个 bug",
        "write a tutorial about decorators",
    ],
)
def test_is_trivial_request_lets_real_asks_through(request_text: str) -> None:
    assert not is_trivial_request(request_text), (
        f"{request_text!r} should NOT be flagged as trivial — it's a real ask"
    )


# --- worth_saving_as_skill (the unified gate) ------------------------------


def test_worth_saving_rejects_trivial_pattern() -> None:
    # The 0.1.45 motivating case: "你好"×3 fires mining but must NOT
    # be saved. This is the test that watches over the user's
    # complaint "你好不值得".
    ok, reason = worth_saving_as_skill(
        "你好",
        plan_subtasks=[_FakeSubtask("general")] * 3,
        had_refusal_retry=False,
    )
    assert ok is False
    assert "trivial" in reason.lower()


def test_worth_saving_rejects_single_subtask_plan() -> None:
    # Even a real-looking question is not a "skill" if it ran as a
    # single subtask — there's no workflow to recall.
    ok, reason = worth_saving_as_skill(
        "what's the capital of France",
        plan_subtasks=[_FakeSubtask("general")],
    )
    assert ok is False
    assert "single-subtask" in reason.lower() or "workflow" in reason.lower()


def test_worth_saving_accepts_refusal_retry_multi_subtask() -> None:
    # Strongest positive signal: citizens had to push past a refusal
    # AND the work was multi-step. This is the 0.1.43 auto-save case.
    ok, _ = worth_saving_as_skill(
        "analyze the failing build and report the root cause",
        plan_subtasks=[_FakeSubtask("research"), _FakeSubtask("analyze")],
        had_refusal_retry=True,
    )
    assert ok is True


def test_worth_saving_accepts_variable_content() -> None:
    # Multi-subtask + a URL in the request → reusable template (next
    # zentao URL will also match). Save it.
    ok, reason = worth_saving_as_skill(
        "analyze https://example.com/zentao/bug-56128.html",
        plan_subtasks=[_FakeSubtask("research"), _FakeSubtask("analyze")],
    )
    assert ok is True
    assert "url" in reason.lower() or "variable" in reason.lower()


def test_worth_saving_accepts_diverse_task_types() -> None:
    # Multi-subtask + different task_types → real decomposition.
    ok, reason = worth_saving_as_skill(
        "review this proposal and write feedback",
        plan_subtasks=[_FakeSubtask("research"), _FakeSubtask("analyze")],
    )
    assert ok is True
    assert "decomposition" in reason.lower() or "task" in reason.lower()


def test_worth_saving_rejects_homogeneous_general_plan() -> None:
    # Multi-subtask but every subtask is "general" + no variable
    # content + no refusal — the plan didn't really specialize, no
    # reuse signal to save.
    ok, reason = worth_saving_as_skill(
        "tell me something interesting",
        plan_subtasks=[_FakeSubtask("general"), _FakeSubtask("general")],
    )
    assert ok is False
    assert "signal" in reason.lower() or "reuse" in reason.lower()


def test_worth_saving_handles_none_plan() -> None:
    # Defensive: None plan_subtasks shouldn't crash, just rejects.
    ok, _ = worth_saving_as_skill("your usual ask")
    assert ok is False
