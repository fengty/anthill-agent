"""Recipe tests — placeholder substitution, TOML round-trip, run wiring.

The recipe layer's value is two things: deterministic placeholder
substitution (KeyError on missing keys, not silent literals), and a
lossless TOML round-trip so the user can hand-edit recipe files
without losing fields. Both are exercised here, plus the Nation.ask
pre_plan path that lets recipes skip Scout entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.recipes import (
    Recipe,
    RecipeSubtask,
    list_recipes,
    load_recipe,
    record_run,
    recipe_path,
    remove_recipe,
    save_recipe,
)


# --- placeholder + substitution -------------------------------------------


def test_placeholders_dedup_and_preserve_order() -> None:
    r = Recipe(
        name="test",
        template="Research {topic} thoroughly. Make {topic} sing. Also: {tone}.",
    )
    assert r.placeholders() == ["topic", "tone"]


def test_placeholders_include_subtask_prompts() -> None:
    r = Recipe(
        name="test",
        template="Process {input}",
        subtasks=[
            RecipeSubtask(task_type="x", prompt_template="Use {filter}"),
        ],
    )
    assert r.placeholders() == ["input", "filter"]


def test_fill_substitutes_template_and_subtasks() -> None:
    r = Recipe(
        name="test",
        template="Research {topic}",
        subtasks=[
            RecipeSubtask(task_type="research", prompt_template="Dig into {topic}"),
            RecipeSubtask(
                task_type="brief", prompt_template="Brief on {topic}",
                depends_on=["research"],
            ),
        ],
    )
    filled = r.fill({"topic": "ants"})
    assert filled.request == "Research ants"
    assert filled.plan is not None
    assert filled.plan.subtasks[0].prompt == "Dig into ants"
    assert filled.plan.subtasks[1].prompt == "Brief on ants"
    assert filled.plan.subtasks[1].depends_on == ["research"]


def test_fill_raises_clearly_on_missing_placeholder() -> None:
    r = Recipe(name="test", template="Need {topic} and {tone}")
    with pytest.raises(KeyError, match="placeholder.*tone"):
        r.fill({"topic": "anything"})


def test_fill_ignores_extra_args() -> None:
    r = Recipe(name="test", template="Hello {name}")
    filled = r.fill({"name": "world", "unused": "ignored"})
    assert filled.request == "Hello world"


def test_simple_recipe_has_no_plan() -> None:
    """A recipe with no subtasks fills to request-only; Scout still plans."""
    r = Recipe(name="t", template="Just ask: {q}")
    filled = r.fill({"q": "what time is it"})
    assert filled.plan is None
    assert filled.request == "Just ask: what time is it"


# --- on-disk TOML round-trip ----------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    r = Recipe(
        name="brief",
        template='Research {topic} and write a "one-pager"',
        description="Quick brief generator",
    )
    save_recipe(r, tmp_path)
    loaded = load_recipe("brief", tmp_path)
    assert loaded is not None
    assert loaded.name == "brief"
    assert loaded.template == 'Research {topic} and write a "one-pager"'
    assert loaded.description == "Quick brief generator"


def test_save_and_load_explicit_subtasks(tmp_path: Path) -> None:
    r = Recipe(
        name="pipeline",
        template="Process {input}",
        subtasks=[
            RecipeSubtask(task_type="parse", prompt_template="Parse {input}"),
            RecipeSubtask(
                task_type="summarize",
                prompt_template="Summarize the parse",
                depends_on=["parse"],
            ),
        ],
    )
    save_recipe(r, tmp_path)
    loaded = load_recipe("pipeline", tmp_path)
    assert loaded is not None
    assert len(loaded.subtasks) == 2
    assert loaded.subtasks[0].task_type == "parse"
    assert loaded.subtasks[1].depends_on == ["parse"]


def test_name_sanitization(tmp_path: Path) -> None:
    """Recipe name with unsafe chars stored under sanitized filename."""
    r = Recipe(name="my recipe v2!", template="hi")
    save_recipe(r, tmp_path)
    path = recipe_path(tmp_path, "my recipe v2!")
    assert path.exists()
    assert path.stem == "my_recipe_v2"


def test_list_recipes_alphabetical(tmp_path: Path) -> None:
    for name in ["zeta", "alpha", "mu"]:
        save_recipe(Recipe(name=name, template="x"), tmp_path)
    names = [r.name for r in list_recipes(tmp_path)]
    assert names == ["alpha", "mu", "zeta"]


def test_remove_recipe(tmp_path: Path) -> None:
    save_recipe(Recipe(name="gone", template="bye"), tmp_path)
    assert remove_recipe("gone", tmp_path) is True
    assert load_recipe("gone", tmp_path) is None


def test_remove_missing_returns_false(tmp_path: Path) -> None:
    assert remove_recipe("ghost", tmp_path) is False


def test_corrupt_recipe_file_is_skipped(tmp_path: Path) -> None:
    """A bad TOML file shouldn't blow up `recipe list`."""
    save_recipe(Recipe(name="good", template="hi"), tmp_path)
    (tmp_path / "recipes" / "broken.toml").write_text("name = \"unclosed")
    items = list_recipes(tmp_path)
    assert [r.name for r in items] == ["good"]


def test_record_run_updates_count_and_timestamp(tmp_path: Path) -> None:
    r = Recipe(name="r", template="x")
    save_recipe(r, tmp_path)
    assert r.run_count == 0
    record_run(r, tmp_path)
    record_run(r, tmp_path)
    reloaded = load_recipe("r", tmp_path)
    assert reloaded is not None
    assert reloaded.run_count == 2
    assert reloaded.last_run_at is not None


# --- escaping edge cases ---------------------------------------------------


def test_double_quotes_in_template_survive(tmp_path: Path) -> None:
    r = Recipe(name="q", template='Use "quotes" and \\backslashes\\')
    save_recipe(r, tmp_path)
    loaded = load_recipe("q", tmp_path)
    assert loaded is not None
    assert loaded.template == 'Use "quotes" and \\backslashes\\'


# --- Nation.ask integration via pre_plan ----------------------------------


@pytest.mark.asyncio
async def test_nation_ask_with_pre_plan_skips_scout(tmp_path: Path) -> None:
    """When pre_plan is provided, Scout should never be invoked."""
    from dataclasses import dataclass as _dc

    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan, Subtask

    @_dc
    class _FakeResult:
        output: str
        success_score: float = 1.0
        agent_id: str = "ant-1"
        task_type: str = ""
        duration_seconds: float = 0.0
        input_tokens: int = 0
        output_tokens: int = 0
        task_id: str = "task-fake"

    n = Nation(name="testnat")
    n.agents = [Agent(model="deepseek-chat", id="ant-1")]

    async def fake_run(task_type, prompt, *, forbid=None):  # noqa: ANN001, ANN201
        return _FakeResult(output=f"<{task_type}>", agent_id="ant-1", task_type=task_type)

    n.run = fake_run  # type: ignore[assignment]

    plan = Plan(
        subtasks=[Subtask(task_type="explicit", prompt="do explicit", depends_on=[])]
    )
    # Crucially, do NOT seed the plan_cache. If Scout were called, the
    # test would fail (no provider configured), so the pass-through is
    # what we're verifying.
    result = await n.ask("user-facing request", pre_plan=plan)
    assert result.plan is plan
    assert result.outcomes[0].status == "ok"
    assert n.last_ask_cache_hit is False
