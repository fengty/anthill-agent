"""v0.5 — structured failure attribution + immune-system quarantine.

Three layers under test:
  1. core.failure.classify_attempt: rule-based classification of LLM
     output / exceptions into structured FailureReason enum
  2. core.immune.CitizenHealth: sliding-window pathology detection
  3. Nation.run end-to-end: failure_reason persisted, auto-quarantine
     triggers, probe-release lifts quarantine
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.agent import Agent
from anthill.core.failure import (
    FailureReason,
    classify_attempt,
    explain,
    is_actionable,
)
from anthill.core.immune import (
    ACTIONABLE_FRACTION,
    FAIL_RATE_THRESHOLD,
    MIN_OBSERVATIONS,
    PROBE_RELEASE_STREAK,
    WINDOW_SIZE,
    AttemptRecord,
    CitizenHealth,
)
from anthill.core.nation import Nation


# --- classify_attempt — pure rule classification ---------------------------


def test_empty_output_classifies_as_empty_response() -> None:
    assert classify_attempt("") == FailureReason.EMPTY_RESPONSE
    assert classify_attempt("   ") == FailureReason.EMPTY_RESPONSE


def test_policy_refusal_english() -> None:
    text = "I'm sorry, but I cannot help with that request."
    assert classify_attempt(text) == FailureReason.POLICY_REFUSAL


def test_policy_refusal_chinese() -> None:
    text = "抱歉，我无法帮助这类问题。"
    assert classify_attempt(text) == FailureReason.POLICY_REFUSAL


def test_timeout_in_output() -> None:
    assert classify_attempt(
        "[error] request timed out after 60s"
    ) == FailureReason.TIMEOUT


def test_timeout_in_exception_class() -> None:
    class _TimeoutError(Exception):
        pass
    assert classify_attempt(
        "[error] something",
        exception=_TimeoutError("boom"),
    ) == FailureReason.TIMEOUT


def test_rate_limit() -> None:
    assert classify_attempt(
        "[error] 429 Too Many Requests"
    ) == FailureReason.RATE_LIMIT


def test_network_error() -> None:
    assert classify_attempt(
        "[error] Connection refused by api.example.com"
    ) == FailureReason.NETWORK


def test_model_error_400() -> None:
    assert classify_attempt(
        "[error] 400 Bad Request from provider"
    ) == FailureReason.MODEL_ERROR


def test_judge_low_only_when_success() -> None:
    """JUDGE_LOW is for the soft-fail path: response was OK but graded poorly."""
    text = "Some response that's not great."
    assert classify_attempt(
        text, success_score=1.0, judge_score=0.2
    ) == FailureReason.JUDGE_LOW


def test_judge_low_not_applied_when_score_zero() -> None:
    """If success_score is already 0, we want the actual reason, not JUDGE_LOW."""
    assert classify_attempt(
        "I'm sorry, but I cannot help.",
        success_score=0.0,
        judge_score=0.1,
    ) == FailureReason.POLICY_REFUSAL


def test_successful_attempt_returns_none() -> None:
    assert classify_attempt(
        "The answer is 42.", success_score=1.0
    ) is None


def test_unclassifiable_falls_through_to_unknown() -> None:
    """Generic non-empty failure text with no matching pattern."""
    assert classify_attempt(
        "wtf this should not happen", success_score=0.0
    ) == FailureReason.UNKNOWN


def test_explain_returns_human_text_for_every_reason() -> None:
    for reason in FailureReason:
        text = explain(reason)
        assert isinstance(text, str) and len(text) > 0


def test_is_actionable_partition() -> None:
    """Citizen-attributable vs environmental."""
    assert is_actionable(FailureReason.POLICY_REFUSAL)
    assert is_actionable(FailureReason.EMPTY_RESPONSE)
    assert is_actionable(FailureReason.MODEL_ERROR)
    assert is_actionable(FailureReason.FORMAT_ERROR)
    # Environmental — citizen isn't at fault
    assert not is_actionable(FailureReason.NETWORK)
    assert not is_actionable(FailureReason.RATE_LIMIT)
    assert not is_actionable(FailureReason.TIMEOUT)


# --- CitizenHealth — sliding-window pathology -----------------------------


def _record(succeeded: bool, reason: FailureReason | None = None) -> AttemptRecord:
    return AttemptRecord(
        timestamp=0.0,
        success_score=1.0 if succeeded else 0.0,
        failure_reason=None if succeeded else (reason or FailureReason.UNKNOWN),
        task_type="x",
    )


def test_empty_window_is_healthy() -> None:
    h = CitizenHealth(agent_id="ant-1")
    assert h.observations == 0
    assert not h.is_pathological()


def test_short_window_under_minimum_is_healthy() -> None:
    """Below MIN_OBSERVATIONS we never declare pathology."""
    h = CitizenHealth(agent_id="ant-1")
    for _ in range(MIN_OBSERVATIONS - 1):
        h.record(_record(False, FailureReason.POLICY_REFUSAL))
    assert not h.is_pathological()


def test_high_fail_rate_with_actionable_dominant_is_pathological() -> None:
    h = CitizenHealth(agent_id="ant-1")
    # 8/10 failures, all POLICY_REFUSAL (actionable) → unhealthy
    for _ in range(8):
        h.record(_record(False, FailureReason.POLICY_REFUSAL))
    for _ in range(2):
        h.record(_record(True))
    assert h.failure_rate == pytest.approx(0.8)
    assert h.is_pathological()


def test_high_fail_rate_with_environmental_failures_is_healthy() -> None:
    """80% failures BUT they're all NETWORK ⇒ not the citizen's fault."""
    h = CitizenHealth(agent_id="ant-1")
    for _ in range(8):
        h.record(_record(False, FailureReason.NETWORK))
    for _ in range(2):
        h.record(_record(True))
    assert h.failure_rate >= 0.6
    assert not h.is_pathological()


def test_mixed_failures_use_actionable_fraction() -> None:
    """If half of failures are actionable, that crosses the threshold."""
    h = CitizenHealth(agent_id="ant-1")
    # 6 fails: 3 actionable (POLICY) + 3 environmental (NETWORK) ⇒ 50% actionable
    # That's at the ACTIONABLE_FRACTION boundary
    for _ in range(3):
        h.record(_record(False, FailureReason.POLICY_REFUSAL))
    for _ in range(3):
        h.record(_record(False, FailureReason.NETWORK))
    for _ in range(4):
        h.record(_record(True))
    assert h.failure_rate == pytest.approx(0.6)
    # 50% actionable, equal threshold → pathological (≥)
    assert h.is_pathological() == (ACTIONABLE_FRACTION <= 0.5)


def test_window_caps_at_window_size() -> None:
    h = CitizenHealth(agent_id="ant-1")
    for i in range(WINDOW_SIZE * 2):
        h.record(_record(i % 2 == 0))
    assert h.observations == WINDOW_SIZE


def test_dominant_reason_returns_most_common() -> None:
    h = CitizenHealth(agent_id="ant-1")
    for _ in range(5):
        h.record(_record(False, FailureReason.POLICY_REFUSAL))
    for _ in range(2):
        h.record(_record(False, FailureReason.NETWORK))
    assert h.dominant_reason() == FailureReason.POLICY_REFUSAL


def test_dominant_reason_none_on_tie() -> None:
    """If two reasons tie, don't claim one — surface uncertainty."""
    h = CitizenHealth(agent_id="ant-1")
    h.record(_record(False, FailureReason.POLICY_REFUSAL))
    h.record(_record(False, FailureReason.NETWORK))
    assert h.dominant_reason() is None


# --- Nation.run integration -----------------------------------------------


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.input_tokens = 10
        self.output_tokens = 10


class _ScriptedProvider:
    """Plays back a list of response strings in order; cycles if exhausted."""

    def __init__(self, scripts: list[str]) -> None:
        self.scripts = scripts
        self.idx = 0

    async def complete(self, *args, **kwargs):  # noqa: ANN001, ANN201, ARG002
        text = self.scripts[self.idx % len(self.scripts)]
        self.idx += 1
        if text.startswith("RAISE:"):
            cls_name = text.split(":", 1)[1]
            ExcClass = type(cls_name, (Exception,), {})
            raise ExcClass("simulated")
        return _FakeResponse(text)


def _nation_with_scripted(scripts: list[str]) -> tuple[Nation, Agent]:
    n = Nation(name="t")
    n.use_judge = False
    a = Agent(id="ant-1", model="deepseek-chat")
    a._provider = _ScriptedProvider(scripts)  # type: ignore[assignment]
    n.agents = [a]
    return n, a


@pytest.mark.asyncio
async def test_run_records_failure_reason_on_taskresult() -> None:
    n, _ = _nation_with_scripted(["I'm sorry, but I cannot help with that."])
    result = await n.run("translate", "anything")
    assert result.success_score == 1.0  # output is non-empty
    # But classify_attempt should still spot the refusal pattern
    assert result.failure_reason == FailureReason.POLICY_REFUSAL.value


@pytest.mark.asyncio
async def test_run_records_empty_when_response_blank() -> None:
    n, _ = _nation_with_scripted([""])
    result = await n.run("x", "y")
    assert result.success_score == 0.0
    assert result.failure_reason == FailureReason.EMPTY_RESPONSE.value


@pytest.mark.asyncio
async def test_run_updates_citizen_health_window() -> None:
    n, a = _nation_with_scripted(["fine response that worked"])
    await n.run("x", "y")
    assert a.id in n.citizen_health
    assert n.citizen_health[a.id].observations == 1


@pytest.mark.asyncio
async def test_auto_quarantine_disabled_by_default() -> None:
    """immune_enabled=False ⇒ pathology detected but no action taken."""
    scripts = ["I cannot help with that."] * 10
    n, a = _nation_with_scripted(scripts)
    # immune_enabled defaults to False
    for _ in range(10):
        await n.run("x", "y")
    assert not a.is_quarantined


@pytest.mark.asyncio
async def test_auto_quarantine_kicks_in_when_enabled() -> None:
    """immune_enabled=True ⇒ a pathological citizen actually gets isolated.

    Once quarantined the router will refuse to pick the same citizen on
    the next call, so we break out of the loop as soon as the flag flips.
    """
    scripts = ["I cannot help with that."] * 20
    n, a = _nation_with_scripted(scripts)
    n.immune_enabled = True
    needed = max(MIN_OBSERVATIONS + 1, int(1 / (1 - FAIL_RATE_THRESHOLD)) + 1)
    for _ in range(needed):
        if a.is_quarantined:
            break
        await n.run("x", "y")
    assert a.is_quarantined
    assert a.quarantine_reason is not None


def test_manual_quarantine_unquarantine() -> None:
    n = Nation(name="t")
    a = Agent(id="ant-1", model="x")
    n.agents = [a]
    assert n.quarantine(a.id, reason="testing") is a
    assert a.is_quarantined
    assert not a.is_available
    assert n.unquarantine(a.id) is a
    assert not a.is_quarantined
    assert a.is_available


def test_quarantine_already_quarantined_returns_none() -> None:
    n = Nation(name="t")
    a = Agent(id="ant-1", model="x")
    n.agents = [a]
    n.quarantine(a.id)
    assert n.quarantine(a.id) is None  # idempotent


def test_unquarantine_unknown_returns_none() -> None:
    n = Nation(name="t")
    assert n.unquarantine("nobody") is None


def test_router_skips_quarantined_citizens() -> None:
    from anthill.core.router import Router, RouterConfig
    n = Nation(name="t")
    healthy = Agent(id="ant-healthy", model="x")
    sick = Agent(id="ant-sick", model="x")
    sick.quarantined_at = 1.0
    n.agents = [healthy, sick]
    n.pheromones.deposit("ant-sick", "x", 1.0)  # stronger trail
    n.pheromones.deposit("ant-sick", "x", 1.0)
    n.pheromones.deposit("ant-healthy", "x", 0.5)
    router = Router(n.pheromones, n.agents, RouterConfig(exploration=0.0))
    # Despite ant-sick having a stronger trail, quarantine excludes it.
    picks = [router.assign("x").id for _ in range(20)]
    assert all(p == "ant-healthy" for p in picks)


@pytest.mark.asyncio
async def test_probe_release_lifts_quarantine_after_streak() -> None:
    """Manually quarantine, then run enough good attempts to release."""
    scripts = ["good response"] * (PROBE_RELEASE_STREAK + 1)
    n, a = _nation_with_scripted(scripts)
    n.quarantine(a.id, reason="testing")
    assert a.is_quarantined
    # We use Router with the agent in quarantine — by default it's
    # excluded. To get the probe path we directly call run with the
    # quarantined citizen as the only candidate; in practice this happens
    # when the user runs `anthill citizen quarantine release` or when
    # only the quarantined citizen remains. We simulate by un-pausing
    # the router via mocking find_agent / executing the path manually:
    for _ in range(PROBE_RELEASE_STREAK):
        # Use agent.execute directly + apply the immune update;
        # otherwise the router would refuse to pick a quarantined ant.
        from anthill.core.immune import (
            maybe_probe_release,
            record_attempt,
        )
        result = await a.execute("x", "y")
        n.pheromones.deposit(a.id, "x", result.success_score)
        health = record_attempt(n, a.id, "x", result)
        maybe_probe_release(n, a, health, result)
    assert not a.is_quarantined  # released after streak


# --- persistence -----------------------------------------------------------


def test_quarantine_fields_round_trip(tmp_path: Path) -> None:
    from anthill.core.persistence import load_nation, save_nation
    n = Nation(name="testnat")
    a = Agent(id="ant-1", model="x")
    a.quarantined_at = 12345.6
    a.quarantine_reason = "policy_refusal dominated"
    n.agents = [a]
    n.immune_enabled = True
    save_nation(n, tmp_path)
    reloaded = load_nation("testnat", tmp_path)
    assert reloaded is not None
    assert reloaded.agents[0].quarantined_at == pytest.approx(12345.6)
    assert reloaded.agents[0].quarantine_reason == "policy_refusal dominated"
    assert reloaded.immune_enabled is True


def test_legacy_nation_without_immune_file_loads_clean(tmp_path: Path) -> None:
    (tmp_path / "nations" / "legacy").mkdir(parents=True)
    (tmp_path / "nations" / "legacy" / "agents.json").write_text("[]")
    (tmp_path / "nations" / "legacy" / "pheromones.json").write_text("[]")
    from anthill.core.persistence import load_nation
    nat = load_nation("legacy", tmp_path)
    assert nat is not None
    assert nat.immune_enabled is False
