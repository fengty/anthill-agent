"""0.1.33 — project-context-relevance gate.

Real-user bug: from inside an `anthill-agent` repo a user typed
"我希望找个 AI 项目研究下" (a general "recommend me an AI project to
study" question). 0.1.15's project-context block was always injected
when a project root was detected, so Scout saw "[project: anthill-
agent]" and confabulated five fictional AI subprojects for the
existing "scaling API". Quality scored 100% because the output
looked coherent.

This patch gates the injection: only when the request actually
references the local project does the block go in. Override via
/project on / off / auto.

Tests cover:
- positive cases that should trigger injection (English + Chinese)
- negative cases that should NOT (general queries with no project ref)
- file-path / @-token edge cases that force-enable
- the on/off/auto mode override in Nation.ask gating
"""

from __future__ import annotations

import pytest


# --- relevance heuristic -------------------------------------------------


def test_general_query_is_not_project_relevant() -> None:
    """The exact bug report: pure external question gets no project block."""
    from anthill.core.project import is_project_relevant_request

    assert is_project_relevant_request("我希望找个 AI 项目研究下") is False
    assert is_project_relevant_request("recommend an AI project to study") is False
    assert is_project_relevant_request("what is stigmergy") is False
    assert is_project_relevant_request("translate this to French") is False


def test_english_this_project_is_relevant() -> None:
    from anthill.core.project import is_project_relevant_request

    assert is_project_relevant_request("refactor this project to use uv") is True
    assert is_project_relevant_request("review the codebase for security") is True
    assert is_project_relevant_request("fix the bug in src/parser.py") is True


def test_chinese_local_project_phrases_are_relevant() -> None:
    from anthill.core.project import is_project_relevant_request

    assert is_project_relevant_request("帮我看下这个项目的架构") is True
    assert is_project_relevant_request("修一下我的代码") is True
    assert is_project_relevant_request("这份代码里有什么 bug") is True


def test_file_path_marker_triggers_relevance() -> None:
    from anthill.core.project import is_project_relevant_request

    assert is_project_relevant_request("add a test for parser.py") is True
    assert is_project_relevant_request("optimize the file under src/") is True


def test_at_token_force_enables_relevance() -> None:
    """@file syntax means the user wants project context regardless."""
    from anthill.core.project import is_project_relevant_request

    assert is_project_relevant_request("explain @src/main.py") is True
    assert is_project_relevant_request("@docs/api.md") is True


def test_empty_input_is_not_relevant() -> None:
    from anthill.core.project import is_project_relevant_request

    assert is_project_relevant_request("") is False
    assert is_project_relevant_request("   ") is False


# --- Nation.project_inject_mode override --------------------------------


def test_nation_default_inject_mode_is_auto() -> None:
    from anthill.core.nation import Nation

    assert Nation(name="t").project_inject_mode == "auto"


@pytest.mark.asyncio
async def test_project_block_omitted_when_not_relevant(monkeypatch, tmp_path) -> None:
    """Auto mode + non-project query → project block is NOT injected
    even though a project root exists at cwd."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    # Make find_project_root return a fake project so we know any
    # injection that DOES happen has to come from our gate.
    from anthill.core.project import ProjectInfo

    fake_info = ProjectInfo(
        root=tmp_path, name="fake-project", kind="Python",
        marker="pyproject.toml",
    )
    monkeypatch.setattr(
        "anthill.core.project.find_project_root", lambda *_a, **_kw: fake_info,
    )

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.project_inject_mode = "auto"

    captured_episodic = []

    async def fake_plan(self, request, **kwargs):
        captured_episodic.append(kwargs.get("episodic_context", ""))
        return _Plan(subtasks=[_Sub("general", request, [])])

    async def fake_run(task_type, prompt, **kwargs):
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="ok", success_score=1.0, duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    await n.ask("深入研究并比较 2026 年最值得学习的开源 AI 项目")
    assert captured_episodic, "Scout.plan wasn't called"
    # The fake project name MUST NOT appear in the episodic context.
    assert "fake-project" not in captured_episodic[0]


@pytest.mark.asyncio
async def test_project_block_injected_when_relevant(monkeypatch, tmp_path) -> None:
    """Auto mode + project-relevant query → project block IS injected."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.project import ProjectInfo
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    fake_info = ProjectInfo(
        root=tmp_path, name="fake-project", kind="Python",
        marker="pyproject.toml",
    )
    monkeypatch.setattr(
        "anthill.core.project.find_project_root", lambda *_a, **_kw: fake_info,
    )

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.project_inject_mode = "auto"

    captured = []

    async def fake_plan(self, request, **kwargs):
        captured.append(kwargs.get("episodic_context", ""))
        return _Plan(subtasks=[_Sub("general", request, [])])

    async def fake_run(task_type, prompt, **kwargs):
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="ok", success_score=1.0, duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    await n.ask("详细分析这个项目的架构并提出重构建议")
    assert "fake-project" in captured[0]


@pytest.mark.asyncio
async def test_mode_off_disables_even_for_relevant_request(monkeypatch, tmp_path) -> None:
    """User explicitly disabled — even refactor-this-code doesn't inject."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.project import ProjectInfo
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    fake_info = ProjectInfo(
        root=tmp_path, name="fake-project", kind="Python",
        marker="pyproject.toml",
    )
    monkeypatch.setattr(
        "anthill.core.project.find_project_root", lambda *_a, **_kw: fake_info,
    )

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.project_inject_mode = "off"

    captured = []

    async def fake_plan(self, request, **kwargs):
        captured.append(kwargs.get("episodic_context", ""))
        return _Plan(subtasks=[_Sub("general", request, [])])

    async def fake_run(task_type, prompt, **kwargs):
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="ok", success_score=1.0, duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    await n.ask("详细分析这个项目的架构并提出重构建议")
    assert "fake-project" not in captured[0]


@pytest.mark.asyncio
async def test_mode_on_forces_inject_for_general_query(monkeypatch, tmp_path) -> None:
    """Forced-on mode injects even when the request looks unrelated."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.project import ProjectInfo
    from anthill.core.scout import Plan as _Plan
    from anthill.core.scout import Scout as _Scout
    from anthill.core.scout import Subtask as _Sub

    fake_info = ProjectInfo(
        root=tmp_path, name="fake-project", kind="Python",
        marker="pyproject.toml",
    )
    monkeypatch.setattr(
        "anthill.core.project.find_project_root", lambda *_a, **_kw: fake_info,
    )

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.project_inject_mode = "on"

    captured = []

    async def fake_plan(self, request, **kwargs):
        captured.append(kwargs.get("episodic_context", ""))
        return _Plan(subtasks=[_Sub("general", request, [])])

    async def fake_run(task_type, prompt, **kwargs):
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="ok", success_score=1.0, duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    await n.ask("深入比较 2026 年的开源 AI 项目")
    assert "fake-project" in captured[0]
