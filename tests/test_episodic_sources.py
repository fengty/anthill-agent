"""0.1.4 — `episodic_sources` on AskResult.

Closes the loop "nation reads past history during planning" by exposing
WHICH past entries were actually pulled. Lets the REPL print
"📚 borrowed from <id1, id2>" so the user sees the memory working.

Tests:
  1. Field default empty (no regressions for legacy callers).
  2. _similar_past_block_with_sources returns matching IDs alongside text.
  3. Nation.ask populates it when similar past entries exist.
  4. Trivial / cache-hit / pre-plan / resume paths leave it empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_askresult_default_is_empty_list() -> None:
    """Legacy callers that don't pass episodic_sources get [], not None."""
    from anthill.core.nation import AskResult
    from anthill.core.scout import Plan

    ar = AskResult(
        request="x",
        plan=Plan(subtasks=[]),
        outcomes=[],
    )
    assert ar.episodic_sources == []
    assert isinstance(ar.episodic_sources, list)


def test_similar_past_block_with_sources_returns_ids(tmp_path: Path) -> None:
    """The new helper returns both text AND a list of HistoryEntry ids."""
    from anthill.core.history import HistoryEntry, append_history
    from anthill.core.nation import Nation

    n = Nation(name="t")
    n.history_path = tmp_path / "history.jsonl"
    # Seed history with two related entries
    for i, req in enumerate(["translate hello to French", "translate goodbye to French"]):
        e = HistoryEntry(
            id=f"hid-{i:04d}", timestamp=float(i), request=req,
            plan=[], outcomes=[],
        )
        append_history(e, tmp_path)

    text, sources = n._similar_past_block_with_sources("translate this to French")
    assert text  # non-empty context block was produced
    # Both prior entries are about the same topic — both should match
    assert any(s.startswith("hid-") for s in sources)


def test_similar_past_block_empty_when_no_history(tmp_path: Path) -> None:
    from anthill.core.nation import Nation
    n = Nation(name="t")
    n.history_path = tmp_path / "history.jsonl"  # file doesn't exist
    text, sources = n._similar_past_block_with_sources("anything")
    assert text == ""
    assert sources == []


def test_similar_past_block_empty_when_no_history_path() -> None:
    """Nations without a history_path stay quiet — no exception."""
    from anthill.core.nation import Nation
    n = Nation(name="t")
    n.history_path = None
    text, sources = n._similar_past_block_with_sources("anything")
    assert text == ""
    assert sources == []


@pytest.mark.asyncio
async def test_nation_ask_records_episodic_sources(monkeypatch, tmp_path: Path) -> None:
    """End-to-end: Scout sees the context block, AskResult exposes the IDs."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.history import HistoryEntry, append_history
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan, Scout as _Scout, Subtask as _Sub

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.history_path = tmp_path / "history.jsonl"
    append_history(
        HistoryEntry(
            id="abc12345", timestamp=1.0,
            request="translate hello to French", plan=[], outcomes=[],
        ),
        tmp_path,
    )

    async def fake_plan(self, request, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _Plan(subtasks=[_Sub("translate", request, [])])

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="bonjour", success_score=1.0, duration_seconds=0.0,
        )

    monkeypatch.setattr(_Scout, "plan", fake_plan)
    n.run = fake_run  # type: ignore[assignment]

    # Non-trivial request that should match the seeded entry
    result = await n.ask("translate goodbye to French today please")
    assert "abc12345" in result.episodic_sources


@pytest.mark.asyncio
async def test_trivial_path_leaves_sources_empty(monkeypatch, tmp_path: Path) -> None:
    """fast_classify=trivial bypasses Scout AND episodic lookup."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.history import HistoryEntry, append_history
    from anthill.core.nation import Nation

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.history_path = tmp_path / "history.jsonl"
    append_history(
        HistoryEntry(
            id="xyz98765", timestamp=1.0,
            request="hi", plan=[], outcomes=[],
        ),
        tmp_path,
    )

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="hello back", success_score=1.0, duration_seconds=0.0,
        )
    n.run = fake_run  # type: ignore[assignment]

    result = await n.ask("hi")  # trivial — Scout bypassed
    assert result.episodic_sources == []


@pytest.mark.asyncio
async def test_pre_plan_path_leaves_sources_empty(tmp_path: Path) -> None:
    """Recipe-driven runs explicitly skip Scout AND its episodic context."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan, Subtask as _Sub

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="done", success_score=1.0, duration_seconds=0.0,
        )
    n.run = fake_run  # type: ignore[assignment]

    plan = _Plan(subtasks=[_Sub("baked", "do it", [])])
    result = await n.ask("anything", pre_plan=plan)
    assert result.episodic_sources == []


@pytest.mark.asyncio
async def test_cache_hit_leaves_sources_empty(monkeypatch, tmp_path: Path) -> None:
    """Plan cache hit ⇒ Scout skipped ⇒ no episodic lookup."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.plan_cache import remember as cache_remember
    from anthill.core.scout import Plan as _Plan, Subtask as _Sub

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    plan = _Plan(subtasks=[_Sub("translate", "do it", [])])
    cache_remember("translate hello to French", plan, n.plan_cache)

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="bonjour", success_score=1.0, duration_seconds=0.0,
        )
    n.run = fake_run  # type: ignore[assignment]

    result = await n.ask("translate hello to French")
    assert result.episodic_sources == []
    assert n.last_ask_cache_hit is True
