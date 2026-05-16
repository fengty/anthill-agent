"""v0.8.0 — deliberation loop tests.

The loop's correctness comes down to four behaviors:
  1. Stops on first round if quality already meets threshold
  2. Critiques + refines + tries again when quality is below threshold
  3. Stops on stagnation when later rounds don't improve enough
  4. Stops at max_rounds; falls back to the BEST round when no win
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

from anthill.core.deliberate import (
    DeliberationRound,
    _quality_of,
    deliberate,
)


# --- _quality_of pure math ------------------------------------------------


@dataclass
class _FakeAttempt:
    success_score: float = 1.0
    scores: dict[str, float] = field(default_factory=dict)


@dataclass
class _FakeOutcome:
    status: str = "ok"
    attempts: list[_FakeAttempt] = field(default_factory=list)


@dataclass
class _FakeAskResult:
    request: str = "r"
    plan: object = None  # not used by _quality_of
    outcomes: list[_FakeOutcome] = field(default_factory=list)
    budget: object = None
    final_output: str = "ok"


def test_quality_of_empty_result_zero() -> None:
    q, dims = _quality_of(_FakeAskResult(outcomes=[]))
    assert q == 0.0
    assert dims == {}


def test_quality_of_no_scores_falls_back_to_success_score() -> None:
    r = _FakeAskResult(outcomes=[
        _FakeOutcome(attempts=[_FakeAttempt(success_score=0.8)]),
        _FakeOutcome(attempts=[_FakeAttempt(success_score=1.0)]),
    ])
    q, dims = _quality_of(r)
    assert q == pytest.approx(0.9)
    assert dims == {}


def test_quality_of_takes_max_across_attempts() -> None:
    """Multiple attempts per outcome — per-dim max wins."""
    r = _FakeAskResult(outcomes=[
        _FakeOutcome(attempts=[
            _FakeAttempt(scores={"correctness": 0.7, "tone": 0.9}),
            _FakeAttempt(scores={"correctness": 0.5, "tone": 0.6}),
        ])
    ])
    q, dims = _quality_of(r)
    assert dims == {"correctness": 0.7, "tone": 0.9}
    assert q == pytest.approx(0.8)


def test_quality_of_excludes_cost_dimension() -> None:
    """`cost` is an efficiency proxy, not a quality signal."""
    r = _FakeAskResult(outcomes=[
        _FakeOutcome(attempts=[_FakeAttempt(scores={
            "correctness": 0.6, "cost": 1.0,  # if cost counted, q→0.8
        })])
    ])
    q, _ = _quality_of(r)
    assert q == pytest.approx(0.6)


def test_quality_of_falls_back_to_cost_when_only_dim() -> None:
    """When cost is the ONLY thing recorded, use it rather than zero."""
    r = _FakeAskResult(outcomes=[
        _FakeOutcome(attempts=[_FakeAttempt(scores={"cost": 0.8})])
    ])
    q, _ = _quality_of(r)
    assert q == pytest.approx(0.8)


def test_quality_of_ignores_failed_outcomes() -> None:
    r = _FakeAskResult(outcomes=[
        _FakeOutcome(status="failed", attempts=[_FakeAttempt(scores={"x": 0.9})]),
        _FakeOutcome(status="ok", attempts=[_FakeAttempt(scores={"x": 0.4})]),
    ])
    q, _ = _quality_of(r)
    assert q == pytest.approx(0.4)


# --- deliberate loop ------------------------------------------------------


class _ScriptedNation:
    """Stub nation: each ask() returns a result whose quality is scripted.

    Quality is set via per-call dim scores. The loop's logic — critique,
    refine, stop conditions — runs verbatim against this stub; only the
    round outputs are controlled.
    """

    def __init__(self, quality_scripts: list[float]) -> None:
        self.quality_scripts = list(quality_scripts)
        self.ask_call_count = 0
        self.run_call_count = 0
        self.last_request_seen: Optional[str] = None

    async def ask(self, request, **kwargs):  # noqa: ANN001, ANN201
        self.last_request_seen = request
        # Each ask gets the next scripted quality. If exhausted, repeat last.
        q = (
            self.quality_scripts[self.ask_call_count]
            if self.ask_call_count < len(self.quality_scripts)
            else self.quality_scripts[-1]
        )
        self.ask_call_count += 1
        outcomes = [
            _FakeOutcome(attempts=[_FakeAttempt(success_score=q, scores={"q": q})])
        ]
        return _FakeAskResult(
            request=request,
            outcomes=outcomes,
            final_output=f"answer for round {self.ask_call_count} (q={q:.2f})",
            budget=None,
        )

    async def run(self, task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        # Called by _make_critique for task_type='review'.
        self.run_call_count += 1
        return _FakeAttempt(success_score=1.0)  # output attr matters elsewhere


class _ScriptedNationWithCritic(_ScriptedNation):
    """Stub that also returns a proper output object from run() for critique."""

    async def run(self, task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        self.run_call_count += 1
        # Critique is short text + has an agent_id
        from anthill.core.agent import TaskResult
        return TaskResult(
            task_id="t", agent_id="ant-critic", task_type=task_type,
            output="- be more concise\n- cite sources",
            success_score=1.0, duration_seconds=0.0,
        )


@pytest.mark.asyncio
async def test_stops_first_round_when_quality_already_high() -> None:
    nation = _ScriptedNation(quality_scripts=[0.9])
    result = await deliberate(nation, "test", quality_threshold=0.85)  # type: ignore[arg-type]
    assert result.total_rounds == 1
    assert result.converged is True
    assert result.stop_reason == "first_round_fine"
    # Critic should NOT have been called
    assert nation.run_call_count == 0


@pytest.mark.asyncio
async def test_runs_to_max_when_quality_never_meets() -> None:
    """Below threshold every round, no stagnation — should reach max."""
    nation = _ScriptedNationWithCritic(quality_scripts=[0.5, 0.6, 0.7, 0.8])
    result = await deliberate(
        nation, "test",  # type: ignore[arg-type]
        quality_threshold=0.95, max_rounds=4,
    )
    assert result.total_rounds == 4
    assert result.stop_reason == "max_rounds"
    assert result.converged is False
    # Final = best so far (0.8 was last + highest)
    assert result.final_round.quality == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_converges_mid_loop_when_threshold_met() -> None:
    """Improvement → improvement → meets threshold."""
    nation = _ScriptedNationWithCritic(quality_scripts=[0.6, 0.75, 0.9])
    result = await deliberate(
        nation, "test",  # type: ignore[arg-type]
        quality_threshold=0.85, max_rounds=5,
    )
    assert result.total_rounds == 3
    assert result.stop_reason == "quality_met"
    assert result.converged is True


@pytest.mark.asyncio
async def test_stops_on_stagnation() -> None:
    """Round 2 doesn't improve enough → bail."""
    nation = _ScriptedNationWithCritic(quality_scripts=[0.5, 0.51])
    result = await deliberate(
        nation, "test",  # type: ignore[arg-type]
        quality_threshold=0.95, max_rounds=5, min_improvement=0.05,
    )
    assert result.stop_reason == "stagnated"
    assert result.total_rounds == 2  # stopped at round 2
    assert result.final_round.quality >= 0.5


@pytest.mark.asyncio
async def test_stops_when_quality_regresses_badly() -> None:
    """Round 2 much worse than Round 1 → take the best (round 1)."""
    nation = _ScriptedNationWithCritic(quality_scripts=[0.7, 0.4])
    result = await deliberate(
        nation, "test",  # type: ignore[arg-type]
        quality_threshold=0.95, max_rounds=5, min_improvement=0.05,
    )
    assert result.stop_reason == "stagnated"
    # final_round should pick the BEST quality, i.e. round 1
    assert result.final_round.round_num == 1
    assert result.final_round.quality == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_critique_text_recorded_on_later_rounds() -> None:
    nation = _ScriptedNationWithCritic(quality_scripts=[0.5, 0.6, 0.85])
    result = await deliberate(
        nation, "test",  # type: ignore[arg-type]
        quality_threshold=0.85, max_rounds=5,
    )
    assert result.rounds[0].critique is None  # round 1 has no critique
    assert result.rounds[1].critique is not None
    assert result.rounds[1].critique_by == "ant-critic"
    assert result.rounds[2].critique is not None


@pytest.mark.asyncio
async def test_refined_request_contains_original_and_critique() -> None:
    nation = _ScriptedNationWithCritic(quality_scripts=[0.5, 0.6])
    await deliberate(
        nation, "find me a recipe",  # type: ignore[arg-type]
        quality_threshold=0.95, max_rounds=2,
    )
    # The 2nd ask's request should contain the original + critique markers
    final_req = nation.last_request_seen or ""
    assert "find me a recipe" in final_req
    assert "CRITIQUE" in final_req
    assert "DRAFT" in final_req


@pytest.mark.asyncio
async def test_on_round_callback_fires_each_round() -> None:
    seen: list[DeliberationRound] = []

    async def grab(r: DeliberationRound) -> None:
        seen.append(r)

    nation = _ScriptedNationWithCritic(quality_scripts=[0.5, 0.6, 0.9])
    await deliberate(
        nation, "test",  # type: ignore[arg-type]
        quality_threshold=0.85, max_rounds=5, on_round=grab,
    )
    assert len(seen) == 3
    assert [r.round_num for r in seen] == [1, 2, 3]


@pytest.mark.asyncio
async def test_quality_trajectory_reflects_each_round() -> None:
    nation = _ScriptedNationWithCritic(quality_scripts=[0.4, 0.6, 0.9])
    result = await deliberate(
        nation, "test",  # type: ignore[arg-type]
        quality_threshold=0.85, max_rounds=5,
    )
    assert result.quality_trajectory == [pytest.approx(0.4),
                                          pytest.approx(0.6),
                                          pytest.approx(0.9)]


@pytest.mark.asyncio
async def test_critique_failure_does_not_break_loop() -> None:
    """A throwing run() (critique) should be caught and recorded as text."""

    class _BrokenCritic(_ScriptedNation):
        async def run(self, task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
            raise RuntimeError("critic provider down")

    nation = _BrokenCritic(quality_scripts=[0.5, 0.6])
    result = await deliberate(
        nation, "test",  # type: ignore[arg-type]
        quality_threshold=0.95, max_rounds=2, min_improvement=0.01,
    )
    assert "[critique unavailable" in (result.rounds[1].critique or "")
