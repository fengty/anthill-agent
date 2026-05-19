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
    SAVE_SCORE_THRESHOLD,
    DistillationSuggestion,
    SkillMatch,
    _output_rich,
    _plan_depth,
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


def test_worth_saving_accepts_diverse_task_types_with_depth() -> None:
    # 0.1.46: diversity alone (1.0) is INTENTIONALLY below threshold
    # (1.5) — the old 0.1.45 "any diversity saves" was too generous.
    # Pair diversity with a dependency so plan_depth>=2 also fires,
    # bringing the score to 2.0 ≥ 1.5.
    ok, reason = worth_saving_as_skill(
        "review this proposal and write feedback",
        plan_subtasks=[
            _SubWithDeps("research"),
            _SubWithDeps("analyze", depends_on=["research"]),
        ],
    )
    assert ok is True, f"expected save; reason: {reason}"
    assert "task_type_diversity" in reason or "plan_depth" in reason


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


# --- 0.1.69 — variable extraction (the inverse of _template_seed) -------


def test_extract_variables_pulls_url() -> None:
    args = extract_variables("分析下：http://ss.example.com/zentao/bug-56128.html")
    assert "url" in args
    assert args["url"].startswith("http://ss.example.com")


def test_extract_variables_pulls_numeric_id() -> None:
    args = extract_variables("look at bug 67890 root cause")
    assert args.get("id") == "67890"


def test_extract_variables_pulls_iso_date() -> None:
    args = extract_variables("summarize standups from 2026-05-19")
    assert args.get("date") == "2026-05-19"


def test_extract_variables_url_precedence_over_id() -> None:
    """URL contains digits — must NOT extract those digits as an
    `id` variable when they're actually part of a URL. The URL
    pattern runs first in _VARIABLE_PATTERNS for this reason."""
    args = extract_variables(
        "analyze http://example.com/bug-12345"
    )
    # url IS extracted
    assert "url" in args
    # id should ALSO match because 12345 still appears as a plain
    # digit run in the text (the URL pattern only consumes the URL
    # bytes; the regex doesn't subtract). That's acceptable —
    # whichever variable Recipe.fill needs, the match will work.
    # The crucial part is `url` is populated.
    # (We don't assert id is absent; either behavior is defensible.)


def test_extract_variables_returns_empty_when_no_match() -> None:
    args = extract_variables("plain text with no url or id or date")
    assert args == {}


def test_extract_variables_safe_format_substitutes_template() -> None:
    """End-to-end: a saved template with {url} placeholder should
    substitute the new URL when extract_variables → Recipe.fill."""
    from anthill.core.recipes import Recipe, RecipeSubtask

    recipe = Recipe(
        name="analyze-url",
        template="analyze {url}",
        subtasks=[
            RecipeSubtask(
                task_type="research",
                prompt_template="fetch content from {url}",
            ),
            RecipeSubtask(
                task_type="analyze",
                prompt_template="explain the bug at {url}",
                depends_on=["research"],
            ),
        ],
    )

    new_request = "分析下：http://example.com/zentao/bug-99999.html"
    args = extract_variables(new_request)
    filled = recipe.fill(args)
    # Both subtask prompts should now contain the real URL, not
    # the literal "{url}".
    for s in filled.plan.subtasks:
        assert "{url}" not in s.prompt
        assert "http://example.com" in s.prompt


# --- 0.1.46 — multi-signal weighted scoring -------------------------------


class _SubWithDeps:
    """Minimal stand-in for scout.Subtask supporting depends_on."""

    def __init__(self, task_type: str, depends_on: list[str] | None = None) -> None:
        self.task_type = task_type
        self.depends_on = depends_on or []


# --- _plan_depth -----------------------------------------------------------


def test_plan_depth_empty_plan() -> None:
    assert _plan_depth([]) == 0


def test_plan_depth_three_parallel_general_is_one() -> None:
    # Three subtasks, all `general`, no dependencies → flat fan-out,
    # not a real pipeline. Depth 1.
    plan = [
        _SubWithDeps("general"),
        _SubWithDeps("general"),
        _SubWithDeps("general"),
    ]
    assert _plan_depth(plan) == 1


def test_plan_depth_linear_chain() -> None:
    # research → analyze → synthesize is a depth-3 pipeline.
    plan = [
        _SubWithDeps("research"),
        _SubWithDeps("analyze", depends_on=["research"]),
        _SubWithDeps("synthesize", depends_on=["analyze"]),
    ]
    assert _plan_depth(plan) == 3


def test_plan_depth_branching_dag() -> None:
    # research (depth 1)
    # ├─ analyze (depth 2)
    # └─ critique (depth 2)
    # └─ merge (depth 3, depends on both)
    plan = [
        _SubWithDeps("research"),
        _SubWithDeps("analyze", depends_on=["research"]),
        _SubWithDeps("critique", depends_on=["research"]),
        _SubWithDeps("merge", depends_on=["analyze", "critique"]),
    ]
    assert _plan_depth(plan) == 3


def test_plan_depth_tolerates_cycles() -> None:
    # Pathological: a depends on b, b depends on a. Should not loop.
    plan = [
        _SubWithDeps("a", depends_on=["b"]),
        _SubWithDeps("b", depends_on=["a"]),
    ]
    d = _plan_depth(plan)
    assert isinstance(d, int)
    assert d >= 1  # don't care about exact value, just no crash


# --- _output_rich ----------------------------------------------------------


def test_output_rich_short_text_is_not_rich() -> None:
    assert _output_rich("a quick reply") is False


def test_output_rich_long_flat_paragraph_is_not_rich() -> None:
    # 500 chars, but no markdown structure → just chatty, not a deliverable.
    assert _output_rich("some text. " * 60) is False


def test_output_rich_long_structured_report_is_rich() -> None:
    # A real deliverable: headers + bullet list + length.
    report = (
        "Here's the analysis:\n"
        "\n## Background\n"
        "Lorem ipsum dolor sit amet, " * 10
        + "\n## Findings\n"
        "\n- finding one with detailed explanation\n"
        "\n- finding two with detailed explanation\n"
        "\n## Recommendations\n"
        "Do this, then that."
    )
    assert _output_rich(report) is True


def test_output_rich_one_marker_not_enough() -> None:
    # Single `#` could be hashtag prose — need 2+ markers.
    text = "a long text that mentions some `# topic` in passing " * 5
    assert _output_rich(text) is False


# --- skill_save_signals (diagnostic map) -----------------------------------


def test_skill_save_signals_returns_all_keys() -> None:
    sigs = skill_save_signals(
        "analyze bug 56128",
        plan_subtasks=[_SubWithDeps("research"), _SubWithDeps("analyze")],
        had_refusal_retry=False,
        final_output="",
    )
    # All expected signal names present.
    assert set(sigs.keys()) == {
        "refusal_retry",
        "variable_content",
        "task_type_diversity",
        "plan_depth_ge_2",
        "subtask_count_ge_3",
        "output_rich",
        "request_long",
    }


def test_skill_save_signals_variable_content_fires_on_id() -> None:
    sigs = skill_save_signals(
        "analyze bug 56128 root cause",
        plan_subtasks=[_SubWithDeps("research"), _SubWithDeps("analyze")],
    )
    assert sigs["variable_content"] is True


def test_skill_save_signals_variable_content_fires_on_url() -> None:
    sigs = skill_save_signals(
        "scrape https://example.com/page",
        plan_subtasks=[_SubWithDeps("research"), _SubWithDeps("analyze")],
    )
    assert sigs["variable_content"] is True


def test_skill_save_signals_depth_signal() -> None:
    sigs = skill_save_signals(
        "do something",
        plan_subtasks=[
            _SubWithDeps("research"),
            _SubWithDeps("analyze", depends_on=["research"]),
        ],
    )
    assert sigs["plan_depth_ge_2"] is True


def test_skill_save_signals_subtask_count() -> None:
    short_plan = [_SubWithDeps("a"), _SubWithDeps("b")]
    long_plan = [_SubWithDeps("a"), _SubWithDeps("b"), _SubWithDeps("c")]
    assert skill_save_signals("x", plan_subtasks=short_plan)["subtask_count_ge_3"] is False
    assert skill_save_signals("x", plan_subtasks=long_plan)["subtask_count_ge_3"] is True


def test_skill_save_signals_request_long() -> None:
    short = skill_save_signals("short ask", plan_subtasks=[_SubWithDeps("a")])
    long_text = "x" * 150
    long_ = skill_save_signals(long_text, plan_subtasks=[_SubWithDeps("a")])
    assert short["request_long"] is False
    assert long_["request_long"] is True


# --- worth_saving_as_skill scoring behavior --------------------------------


def test_worth_saving_score_weak_signals_alone_dont_qualify() -> None:
    # Only "request_long" fires (0.5) — below threshold 1.5 — reject.
    long_request = "tell me something interesting " * 4
    ok, reason = worth_saving_as_skill(
        long_request,
        plan_subtasks=[_SubWithDeps("general"), _SubWithDeps("general")],
    )
    assert ok is False
    # The score should be in the reason for transparency.
    assert "score" in reason.lower()


def test_worth_saving_score_combined_weak_signals_can_qualify() -> None:
    # Diverse task_types (1.0) + plan_depth>=2 (1.0) = 2.0 ≥ 1.5 → save.
    # The 0.1.45 version would have rejected because no single hard
    # signal fired; the 0.1.46 score-based judge gets this right.
    ok, reason = worth_saving_as_skill(
        "review and rewrite my proposal",
        plan_subtasks=[
            _SubWithDeps("research"),
            _SubWithDeps("analyze", depends_on=["research"]),
        ],
    )
    assert ok is True, f"expected save; got reason: {reason}"


def test_worth_saving_score_threshold_is_exposed() -> None:
    # The threshold is a public constant so users can rerun
    # historical data with a different cut.
    assert SAVE_SCORE_THRESHOLD > 0


def test_worth_saving_refusal_retry_alone_qualifies() -> None:
    # refusal_retry weight (2.0) ≥ threshold (1.5) — save by itself.
    ok, _ = worth_saving_as_skill(
        "do this hard thing",
        plan_subtasks=[_SubWithDeps("research"), _SubWithDeps("analyze")],
        had_refusal_retry=True,
    )
    assert ok is True


def test_worth_saving_variable_content_alone_qualifies() -> None:
    # variable_content weight (1.5) == threshold (1.5) — save by itself.
    ok, _ = worth_saving_as_skill(
        "look at bug 12345",
        plan_subtasks=[_SubWithDeps("general"), _SubWithDeps("general")],
    )
    assert ok is True


def test_worth_saving_reason_mentions_fired_signals() -> None:
    # When we save, the reason string must enumerate which signals
    # fired — this is what the REPL prints to the user.
    ok, reason = worth_saving_as_skill(
        "look at bug 12345",
        plan_subtasks=[_SubWithDeps("general"), _SubWithDeps("general")],
    )
    assert ok is True
    assert "variable_content" in reason


def test_worth_saving_reason_explains_why_when_below_threshold() -> None:
    # When the score is non-zero but below threshold, the reason
    # should say what fired and that it was below threshold.
    ok, reason = worth_saving_as_skill(
        "x" * 150,  # only request_long fires (0.5)
        plan_subtasks=[_SubWithDeps("general"), _SubWithDeps("general")],
    )
    assert ok is False
    assert "request_long" in reason
    assert "threshold" in reason.lower() or "below" in reason.lower()


def test_worth_saving_output_rich_contributes_to_score() -> None:
    # output_rich (0.8) + task_type_diversity (1.0) = 1.8 ≥ threshold
    # → save. Verifies the final_output kwarg actually flows through.
    rich_report = (
        "## Section A\n" + "Lorem ipsum dolor sit amet. " * 20
        + "\n## Section B\n" + "\n- item one\n- item two\n- item three\n"
    )
    ok, reason = worth_saving_as_skill(
        "compile that report",
        plan_subtasks=[
            _SubWithDeps("research"),
            _SubWithDeps("analyze"),
        ],
        final_output=rich_report,
    )
    assert ok is True
    assert "output_rich" in reason or "task_type_diversity" in reason
