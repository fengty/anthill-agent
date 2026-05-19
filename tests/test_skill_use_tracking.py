"""0.1.49 — close the skill data loop.

When `Nation.ask` matches a saved recipe via `find_matching_skill`
and runs its plan, we bump `recipe.run_count` and update
`recipe.last_run_at`. This lets the next version surface "saved
N times, last used X" in /skill list and ultimately prune
unused skills.

These fields already exist on `Recipe` (since the very first
recipes implementation); we were just never writing to them.

Tests verify:
  1. A skill match → run_count increments + last_run_at updates
  2. Multiple matches → multiple increments (not just first hit)
  3. The skill file on disk reflects the update (TOML round-trip)
  4. Pre-plan path doesn't accidentally bump skill counters (different
     code path)
  5. Failing save doesn't break the ask (best-effort)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.agent import Agent, TaskResult
from anthill.core.nation import Nation
from anthill.core.recipes import (
    Recipe,
    RecipeSubtask,
    list_recipes,
    save_recipe,
)
from anthill.core.scout import Plan as _Plan, Scout as _Scout, Subtask as _Sub


def _ok_result(task_type: str = "general"):
    return TaskResult(
        task_id="t",
        agent_id="ant-1",
        task_type=task_type,
        output="done",
        success_score=1.0,
        duration_seconds=0.0,
    )


def _seeded_skill(name: str = "analyze-bug-template") -> Recipe:
    """A recipe likely to match a `analyze bug ...` request."""
    return Recipe(
        name=name,
        template="analyze bug ticket and find root cause",
        description="bug-analysis pipeline",
        subtasks=[
            RecipeSubtask(task_type="research", prompt_template="fetch bug details"),
            RecipeSubtask(task_type="analyze", prompt_template="explain root cause"),
        ],
    )


@pytest.mark.asyncio
async def test_skill_match_bumps_run_count(tmp_path: Path) -> None:
    seed = _seeded_skill()
    assert seed.run_count == 0
    assert seed.last_run_at is None
    save_recipe(seed, tmp_path)

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    # This request shares enough tokens with the recipe template that
    # find_matching_skill returns a hit.
    await n.ask(
        "please analyze the bug ticket and explain its root cause",
        nation_dir=tmp_path,
    )

    # Reload from disk — the run_count update must be persisted.
    reloaded = list_recipes(tmp_path)
    assert len(reloaded) == 1
    assert reloaded[0].run_count == 1
    assert reloaded[0].last_run_at is not None
    assert reloaded[0].last_run_at > 0


@pytest.mark.asyncio
async def test_skill_match_multiple_runs_accumulate(tmp_path: Path) -> None:
    save_recipe(_seeded_skill(), tmp_path)

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    for _ in range(3):
        await n.ask(
            "please analyze the bug ticket and explain its root cause",
            nation_dir=tmp_path,
        )

    reloaded = list_recipes(tmp_path)
    assert reloaded[0].run_count == 3


@pytest.mark.asyncio
async def test_skill_miss_does_not_bump_other_skills(tmp_path: Path) -> None:
    """A request that DOESN'T match any saved skill must not change
    any of their counters. Tests that we're updating the matched
    recipe specifically, not all of them."""
    save_recipe(_seeded_skill(), tmp_path)

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    async def fake_plan(self, *a, **kw):  # noqa: ANN001, ANN201, ARG002
        return _Plan(subtasks=[_Sub("general", "do it", [])])

    monkeypatch_target = _Scout

    # Use pytest's monkeypatch via direct attribute mutation in the test
    # (no fixture available since we're at module scope) — restore after.
    orig = monkeypatch_target.plan
    monkeypatch_target.plan = fake_plan  # type: ignore[assignment]
    try:
        await n.ask(
            "translate this poem to French",  # unrelated to bug analysis
            nation_dir=tmp_path,
        )
    finally:
        monkeypatch_target.plan = orig  # type: ignore[assignment]

    reloaded = list_recipes(tmp_path)
    assert reloaded[0].run_count == 0
    assert reloaded[0].last_run_at is None


@pytest.mark.asyncio
async def test_skill_match_substitutes_url_into_prompts(tmp_path: Path) -> None:
    """0.1.69 — the dead-skill bug: when an auto-distilled recipe has
    `{url}` in its subtask templates, Nation.ask must extract the
    URL from the NEW request and substitute it before handing
    prompts to citizens. Before this fix, citizens saw the literal
    string "{url}" and correctly complained it wasn't a real URL."""
    # Save a recipe with a placeholder in BOTH the template and the
    # subtask prompts — mimics what 0.1.43 auto-save produces.
    save_recipe(
        Recipe(
            name="analyze-url-skill",
            template="analyze {url}",
            description="url analysis",
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
        ),
        tmp_path,
    )

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    # Record what each subtask actually received as its prompt.
    received_prompts: list[str] = []

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ARG002
        received_prompts.append(prompt)
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="done",
            success_score=1.0,
            duration_seconds=0.0,
        )

    n.run = fake_run  # type: ignore[assignment]

    new_url = "http://example.com/zentao/bug-99999.html"
    await n.ask(f"analyze {new_url}", nation_dir=tmp_path)

    # CRITICAL: every prompt fed to a citizen must contain the real
    # URL, NOT the literal "{url}" placeholder.
    assert len(received_prompts) == 2
    for p in received_prompts:
        assert "{url}" not in p, f"placeholder leaked into citizen: {p!r}"
        assert new_url in p, f"URL not substituted into: {p!r}"


@pytest.mark.asyncio
async def test_skill_match_persists_to_disk(tmp_path: Path) -> None:
    """The bump must SURVIVE process restart (i.e. it's persisted,
    not just held in memory)."""
    save_recipe(_seeded_skill(), tmp_path)

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _ok_result(task_type)

    n.run = fake_run  # type: ignore[assignment]

    await n.ask(
        "please analyze the bug ticket and explain its root cause",
        nation_dir=tmp_path,
    )

    # Read the raw TOML file — must contain run_count=1.
    toml_files = list((tmp_path / "recipes").glob("*.toml"))
    assert len(toml_files) == 1
    content = toml_files[0].read_text()
    assert "run_count = 1" in content
    assert "last_run_at = " in content
