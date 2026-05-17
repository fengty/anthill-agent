"""Inflight checkpoint tests — serialization, atomic writes, and resume semantics.

Two halves: pure I/O round-trips of the dataclasses, and end-to-end
integration where Nation.ask interruption and resume work as advertised
against a stubbed Nation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from anthill.core.executor import ProgressEvent, SubtaskOutcome, execute_plan
from anthill.core.inflight import (
    CompletedStep,
    InflightAsk,
    clear_inflight,
    inflight_path,
    list_inflight,
    load_inflight,
    save_inflight,
)
from anthill.core.scout import Plan, Subtask


# --- helpers ---------------------------------------------------------------

def _plan(*specs: tuple[str, list[str]]) -> Plan:
    return Plan(
        subtasks=[
            Subtask(task_type=tt, prompt=f"do {tt}", depends_on=list(deps))
            for tt, deps in specs
        ]
    )


@dataclass
class _FakeResult:
    output: str
    success_score: float = 1.0
    agent_id: str = "ant-fake"
    task_type: str = ""
    duration_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    task_id: str = "task-fake"


class _FakeNation:
    """Minimal nation stub. Records every prompt the executor sends it."""

    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self._outputs = outputs or {}
        self.calls: list[tuple[str, str]] = []

    async def run(self, task_type: str, prompt: str, *, forbid=None, on_token=None, **_kw):  # noqa: ANN201
        self.calls.append((task_type, prompt))
        return _FakeResult(
            output=self._outputs.get(task_type, f"<{task_type}>"),
            agent_id="ant-1",
            task_type=task_type,
        )


# --- Round-trip serialization ---------------------------------------------


def test_inflight_round_trip_preserves_plan_and_completed() -> None:
    plan = _plan(("research", []), ("draft", ["research"]))
    ask = InflightAsk.new("test request", plan)
    ask.record_completed(
        CompletedStep(
            index=0,
            task_type="research",
            output="findings",
            agent_id="ant-7",
            started_at=1.0,
            ended_at=2.5,
            attempts=2,
            success_score=0.9,
            input_tokens=100,
            output_tokens=50,
        )
    )

    rehydrated = InflightAsk.from_dict(json.loads(json.dumps(ask.to_dict())))

    assert rehydrated.ask_id == ask.ask_id
    assert rehydrated.request == "test request"
    assert len(rehydrated.plan.subtasks) == 2
    assert rehydrated.plan.subtasks[1].depends_on == ["research"]
    assert len(rehydrated.completed) == 1
    step = rehydrated.completed[0]
    assert step.output == "findings"
    assert step.agent_id == "ant-7"
    assert step.attempts == 2


def test_save_and_load_inflight(tmp_path: Path) -> None:
    plan = _plan(("explain", []))
    ask = InflightAsk.new("hi", plan)
    save_inflight(ask, tmp_path)

    loaded = load_inflight(ask.ask_id, tmp_path)
    assert loaded is not None
    assert loaded.ask_id == ask.ask_id
    assert loaded.request == "hi"


def test_load_inflight_supports_prefix_match(tmp_path: Path) -> None:
    plan = _plan(("x", []))
    ask = InflightAsk.new("anything", plan)
    save_inflight(ask, tmp_path)

    short = ask.ask_id[:3]
    loaded = load_inflight(short, tmp_path)
    assert loaded is not None
    assert loaded.ask_id == ask.ask_id


def test_list_inflight_newest_first(tmp_path: Path) -> None:
    older = InflightAsk(
        ask_id="aaaa1111",
        request="older",
        started_at=100.0,
        plan=_plan(("a", [])),
    )
    newer = InflightAsk(
        ask_id="bbbb2222",
        request="newer",
        started_at=200.0,
        plan=_plan(("a", [])),
    )
    save_inflight(older, tmp_path)
    save_inflight(newer, tmp_path)

    listing = list_inflight(tmp_path)
    assert [a.ask_id for a in listing] == ["bbbb2222", "aaaa1111"]


def test_clear_inflight_removes_file(tmp_path: Path) -> None:
    ask = InflightAsk.new("transient", _plan(("x", [])))
    save_inflight(ask, tmp_path)
    assert inflight_path(tmp_path, ask.ask_id).exists()

    assert clear_inflight(ask.ask_id, tmp_path) is True
    assert not inflight_path(tmp_path, ask.ask_id).exists()


def test_clear_inflight_returns_false_when_missing(tmp_path: Path) -> None:
    assert clear_inflight("nonexistent", tmp_path) is False


def test_corrupt_file_is_skipped_by_list(tmp_path: Path) -> None:
    """A truncated/garbled file shouldn't break `inflight list`."""
    good = InflightAsk.new("good", _plan(("x", [])))
    save_inflight(good, tmp_path)

    (tmp_path / "inflight").mkdir(exist_ok=True)
    (tmp_path / "inflight" / "broken.json").write_text("{not valid json")

    listing = list_inflight(tmp_path)
    assert len(listing) == 1
    assert listing[0].ask_id == good.ask_id


def test_record_completed_replaces_same_index(tmp_path: Path) -> None:
    """Defensive: re-recording the same index should overwrite, not duplicate."""
    ask = InflightAsk.new("r", _plan(("a", []), ("b", [])))
    step = CompletedStep(
        index=0, task_type="a", output="first", agent_id="x",
        started_at=0.0, ended_at=1.0,
    )
    ask.record_completed(step)
    ask.record_completed(
        CompletedStep(
            index=0, task_type="a", output="second", agent_id="x",
            started_at=0.0, ended_at=1.0,
        )
    )
    assert len(ask.completed) == 1
    assert ask.completed[0].output == "second"


def test_latest_by_type_returns_most_recent(tmp_path: Path) -> None:
    """When two steps share a task_type, the later index wins."""
    ask = InflightAsk.new("r", _plan(("a", []), ("a", []), ("b", ["a"])))
    ask.record_completed(
        CompletedStep(index=0, task_type="a", output="first",
                      agent_id="x", started_at=0.0, ended_at=1.0)
    )
    ask.record_completed(
        CompletedStep(index=1, task_type="a", output="second",
                      agent_id="x", started_at=2.0, ended_at=3.0)
    )
    by_type = ask.latest_by_type()
    assert by_type == {"a": "second"}


# --- Executor resume integration ------------------------------------------


@pytest.mark.asyncio
async def test_execute_plan_skips_resumed_subtasks() -> None:
    """When resume_state pre-completes a subtask, the executor must not re-run it."""
    plan = _plan(("research", []), ("draft", ["research"]))
    nation = _FakeNation({"draft": "drafted"})

    pre = _FakeResult(
        output="findings from before crash",
        agent_id="ant-saved",
        task_type="research",
    )
    resume_state = {
        0: SubtaskOutcome(
            subtask=plan.subtasks[0],
            attempts=[pre],
            status="ok",
            started_at=1.0,
            ended_at=2.0,
        )
    }
    outcomes = await execute_plan(plan, nation, resume_state=resume_state)  # type: ignore[arg-type]

    # Only draft should have been called — research was pre-seeded.
    assert [tt for tt, _ in nation.calls] == ["draft"]
    # The draft's prompt must carry forward the resumed research output.
    draft_prompt = nation.calls[0][1]
    assert "findings from before crash" in draft_prompt
    assert outcomes[0].status == "ok"
    assert outcomes[0].attempts[0].agent_id == "ant-saved"
    assert outcomes[1].status == "ok"


@pytest.mark.asyncio
async def test_execute_plan_emits_finished_for_resumed_subtasks() -> None:
    """The UI needs to know about resumed steps too — they emit 'finished'."""
    plan = _plan(("a", []), ("b", ["a"]))
    nation = _FakeNation({"b": "bee"})

    events: list[ProgressEvent] = []

    async def collect(e: ProgressEvent) -> None:
        events.append(e)

    pre = _FakeResult(output="aye", agent_id="ant-old", task_type="a")
    resume_state = {
        0: SubtaskOutcome(
            subtask=plan.subtasks[0],
            attempts=[pre],
            status="ok",
            started_at=0.0,
            ended_at=1.0,
        )
    }
    await execute_plan(plan, nation, on_progress=collect, resume_state=resume_state)  # type: ignore[arg-type]

    # Sequence: finished(resumed) → started(b) → attempt(b) → finished(b).
    kinds = [(e.kind, e.index) for e in events]
    assert kinds[0] == ("finished", 0)  # the resumed one comes first
    assert ("started", 1) in kinds
    assert ("finished", 1) in kinds


@pytest.mark.asyncio
async def test_execute_plan_ignores_non_ok_resume_entries() -> None:
    """Defensive: a failed/skipped pre-seeded outcome is discarded — we retry."""
    plan = _plan(("a", []))
    nation = _FakeNation({"a": "ran fresh"})

    bad = _FakeResult(output="nope", agent_id="ant-fail", task_type="a", success_score=0.0)
    resume_state = {
        0: SubtaskOutcome(
            subtask=plan.subtasks[0],
            attempts=[bad],
            status="failed",
            started_at=0.0,
            ended_at=1.0,
        )
    }
    outcomes = await execute_plan(plan, nation, resume_state=resume_state)  # type: ignore[arg-type]

    # The executor should have rerun the subtask, not honored the failed state.
    assert len(nation.calls) == 1
    assert outcomes[0].status == "ok"
    assert outcomes[0].attempts[-1].output == "ran fresh"


# --- Nation.ask + checkpoint integration ----------------------------------


def _make_real_nation() -> "object":
    """Build a real Nation but with a FakeNation-style overridden `run`."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation

    n = Nation(name="testnat")
    n.agents = [Agent(model="fake", id="ant-1")]
    call_log: list[tuple[str, str]] = []

    async def fake_run(task_type: str, prompt: str, *, forbid=None, on_token=None, **_kw):  # noqa: ANN201
        call_log.append((task_type, prompt))
        return _FakeResult(output=f"<{task_type}>", agent_id="ant-1", task_type=task_type)

    n.run = fake_run  # type: ignore[assignment]
    n._call_log = call_log  # type: ignore[attr-defined]
    return n


@pytest.mark.asyncio
async def test_nation_ask_writes_then_clears_checkpoint(tmp_path: Path) -> None:
    """A clean ask leaves no checkpoint behind."""
    from anthill.core.scout import Plan as _Plan, Subtask as _Sub

    nat = _make_real_nation()
    # Seed plan_cache so Scout doesn't get called.
    plan = _Plan(subtasks=[_Sub(task_type="x", prompt="hi", depends_on=[])])
    from anthill.core.plan_cache import remember as cache_remember
    cache_remember("hello", plan, nat.plan_cache)  # type: ignore[attr-defined]

    result = await nat.ask("hello", nation_dir=tmp_path)  # type: ignore[attr-defined]
    assert result.succeeded
    assert result.ask_id is not None
    # File should be gone — clean completion wipes the checkpoint.
    assert list_inflight(tmp_path) == []


@pytest.mark.asyncio
async def test_nation_ask_can_resume_from_inflight(tmp_path: Path) -> None:
    """End-to-end: pre-seed an inflight with step 0 done, resume runs only step 1."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan, Subtask as _Sub

    n = Nation(name="testnat")
    n.agents = [Agent(model="fake", id="ant-1")]
    calls: list[tuple[str, str]] = []

    async def fake_run(task_type: str, prompt: str, *, forbid=None, on_token=None, **_kw):  # noqa: ANN201
        calls.append((task_type, prompt))
        return _FakeResult(
            output=f"fresh-{task_type}", agent_id="ant-1", task_type=task_type
        )

    n.run = fake_run  # type: ignore[assignment]

    plan = _Plan(
        subtasks=[
            _Sub(task_type="research", prompt="dig", depends_on=[]),
            _Sub(task_type="draft", prompt="write", depends_on=["research"]),
        ]
    )
    inflight = InflightAsk(
        ask_id="resume01",
        request="research and draft",
        started_at=1.0,
        plan=plan,
        completed=[
            CompletedStep(
                index=0,
                task_type="research",
                output="saved findings",
                agent_id="ant-prev",
                started_at=0.0,
                ended_at=1.0,
            )
        ],
    )
    save_inflight(inflight, tmp_path)

    result = await n.ask(  # type: ignore[arg-type]
        "research and draft",
        resume=inflight,
        nation_dir=tmp_path,
    )

    # Only the draft was actually executed.
    assert [tt for tt, _ in calls] == ["draft"]
    # The draft saw the saved research output.
    assert "saved findings" in calls[0][1]
    # Checkpoint cleared after clean completion.
    assert list_inflight(tmp_path) == []
    # The first outcome retains the resumed agent_id, not a fresh one.
    assert result.outcomes[0].attempts[0].agent_id == "ant-prev"
    assert result.outcomes[1].status == "ok"
    assert result.ask_id == "resume01"


@pytest.mark.asyncio
async def test_nation_ask_checkpoints_each_completed_subtask(tmp_path: Path) -> None:
    """After each ok subtask the file should grow; final state has all steps."""
    from anthill.core.agent import Agent
    from anthill.core.nation import Nation
    from anthill.core.plan_cache import remember as cache_remember
    from anthill.core.scout import Plan as _Plan, Subtask as _Sub

    n = Nation(name="testnat")
    n.agents = [Agent(model="fake", id="ant-1")]
    snapshots: list[int] = []

    async def fake_run(task_type: str, prompt: str, *, forbid=None, on_token=None, **_kw):  # noqa: ANN201
        # Sample the checkpoint state every time the executor calls run().
        listing = list_inflight(tmp_path)
        snapshots.append(len(listing[0].completed) if listing else -1)
        return _FakeResult(output=f"<{task_type}>", agent_id="ant-1", task_type=task_type)

    n.run = fake_run  # type: ignore[assignment]

    plan = _Plan(
        subtasks=[
            _Sub(task_type="a", prompt="do a", depends_on=[]),
            _Sub(task_type="b", prompt="do b", depends_on=["a"]),
            _Sub(task_type="c", prompt="do c", depends_on=["b"]),
        ]
    )
    cache_remember("trio", plan, n.plan_cache)

    await n.ask("trio", nation_dir=tmp_path)

    # Before each run-call the checkpoint should reflect prior completions:
    # a runs first (0 done), then b (1 done), then c (2 done).
    assert snapshots == [0, 1, 2]
    assert list_inflight(tmp_path) == []  # cleared after success
