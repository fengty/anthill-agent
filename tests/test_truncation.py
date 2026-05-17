"""0.1.26 — truncation detection end-to-end.

Real user hit it: research-shaped ask produced 6 bullets, output
stopped mid-sentence on "MIT CSAIL: https://csail.mit.edu — MIT's
Computer". Judge gave 100% and the deliberation loop declared
"first_round_fine". Bug chain:

  1. Default max_tokens was 1024 — too small for a research answer
  2. Providers didn't surface finish_reason
  3. Agent.execute didn't know the response was truncated
  4. Judge / quality calc ignored it

This patch bumps default to 4096, threads finish_reason through the
provider layer, sets TaskResult.truncated, caps success_score at 0.5
for truncated attempts, marks failure_reason="truncated", and caps
overall deliberation quality at 0.6 so the loop keeps going.
"""

from __future__ import annotations

import pytest


# --- ModelResponse / StreamChunk fields ----------------------------------


def test_modelresponse_truncated_property_for_length() -> None:
    from anthill.models.base import ModelResponse

    r = ModelResponse(text="x", model="m", finish_reason="length")
    assert r.truncated is True


def test_modelresponse_truncated_property_for_max_tokens() -> None:
    """Anthropic-style raw 'max_tokens' string also counts as truncation."""
    from anthill.models.base import ModelResponse

    r = ModelResponse(text="x", model="m", finish_reason="max_tokens")
    assert r.truncated is True


def test_modelresponse_truncated_property_false_for_stop() -> None:
    from anthill.models.base import ModelResponse

    r = ModelResponse(text="x", model="m", finish_reason="stop")
    assert r.truncated is False


def test_modelresponse_truncated_property_false_for_none() -> None:
    """Providers that don't report it shouldn't be flagged as truncated."""
    from anthill.models.base import ModelResponse

    r = ModelResponse(text="x", model="m", finish_reason=None)
    assert r.truncated is False


def test_default_max_tokens_bumped() -> None:
    """0.1.26: 1024 → 4096 so research outputs don't get clipped."""
    from anthill.models.base import DEFAULT_MAX_TOKENS

    assert DEFAULT_MAX_TOKENS >= 4096


# --- Agent.execute truncation handling -----------------------------------


class _TruncatingProvider:
    """Scripted provider whose response is flagged as truncated."""

    name = "scripted"

    async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
        from anthill.models.base import ModelResponse

        return ModelResponse(
            text="...output ends here on MIT's Computer",
            model="m",
            input_tokens=10,
            output_tokens=max_tokens,
            finish_reason="length",
        )

    async def stream(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
        from anthill.models.base import StreamChunk

        yield StreamChunk(delta="...output ends here on MIT's Computer")
        yield StreamChunk(
            done=True,
            input_tokens=10,
            output_tokens=max_tokens,
            finish_reason="length",
        )


@pytest.mark.asyncio
async def test_agent_marks_truncated_and_penalizes_success_score() -> None:
    from anthill.core.agent import Agent

    a = Agent(id="ant-1", model="scripted")
    a._provider = _TruncatingProvider()

    result = await a.execute("research", "find AI sites")
    assert result.truncated is True
    assert result.success_score == 0.5  # capped, not 1.0
    assert result.failure_reason == "truncated"


@pytest.mark.asyncio
async def test_agent_streaming_path_marks_truncated() -> None:
    """The on_token path must also pick up finish_reason from the
    terminal StreamChunk — not just the non-streaming complete()."""
    from anthill.core.agent import Agent

    a = Agent(id="ant-1", model="scripted")
    a._provider = _TruncatingProvider()

    async def on_token(_d): pass

    result = await a.execute("research", "find AI sites", on_token=on_token)
    assert result.truncated is True
    assert result.failure_reason == "truncated"


# --- Provider finish_reason wiring (mocked httpx) ------------------------


class _FakeOpenAIResp:
    """Stand-in for an httpx POST response carrying a finish_reason."""

    status_code = 200

    def raise_for_status(self): pass

    def json(self):
        return {
            "choices": [
                {
                    "message": {"content": "abc"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4096},
        }


class _FakeClient:
    def __init__(self, *a, **k): pass

    async def __aenter__(self): return self

    async def __aexit__(self, *a): pass

    async def post(self, url, json=None, headers=None):
        return _FakeOpenAIResp()


@pytest.mark.asyncio
async def test_openai_provider_extracts_finish_reason(monkeypatch) -> None:
    import httpx

    from anthill.models.openai_compatible import OpenAICompatibleProvider

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    p = OpenAICompatibleProvider(
        api_key="k", model="m", base_url="https://x/v1", provider_name="openai",
    )
    resp = await p.complete("hi")
    assert resp.finish_reason == "length"
    assert resp.truncated is True


# --- _quality_of truncation cap ------------------------------------------


def test_quality_of_caps_truncated_outcome_at_06() -> None:
    """The exact deliberation bug from the user report: judge gives
    1.0 across all dimensions but the winning attempt is truncated.
    Overall quality should NOT round to 100% — it must cap so the
    loop runs another round."""
    from anthill.core.agent import TaskResult
    from anthill.core.deliberate import _quality_of
    from anthill.core.executor import SubtaskOutcome
    from anthill.core.nation import AskResult
    from anthill.core.scout import Plan, Subtask

    attempt = TaskResult(
        task_id="t",
        agent_id="ant-1",
        task_type="research",
        output="cut off mid sentence on MIT's Computer",
        success_score=0.5,
        duration_seconds=0.0,
        scores={"completeness": 1.0, "correctness": 1.0},
        truncated=True,
    )
    plan = Plan(subtasks=[Subtask("research", "find AI sites", [])])
    outcome = SubtaskOutcome(subtask=plan.subtasks[0], attempts=[attempt], status="ok")
    result = AskResult(request="x", plan=plan, outcomes=[outcome])

    q, _by_dim = _quality_of(result)
    assert q <= 0.6, f"truncated outcome scored {q} (should cap at 0.6)"


def test_quality_of_not_capped_when_no_truncation() -> None:
    """Mirror: non-truncated outcomes still get their full judge score."""
    from anthill.core.agent import TaskResult
    from anthill.core.deliberate import _quality_of
    from anthill.core.executor import SubtaskOutcome
    from anthill.core.nation import AskResult
    from anthill.core.scout import Plan, Subtask

    attempt = TaskResult(
        task_id="t",
        agent_id="ant-1",
        task_type="research",
        output="full clean answer",
        success_score=1.0,
        duration_seconds=0.0,
        scores={"completeness": 1.0, "correctness": 1.0},
        truncated=False,
    )
    plan = Plan(subtasks=[Subtask("research", "find AI sites", [])])
    outcome = SubtaskOutcome(subtask=plan.subtasks[0], attempts=[attempt], status="ok")
    result = AskResult(request="x", plan=plan, outcomes=[outcome])

    q, _by_dim = _quality_of(result)
    assert q == pytest.approx(1.0)


# --- FailureReason.TRUNCATED string round-trip ---------------------------


def test_failure_reason_truncated_explain() -> None:
    from anthill.core.failure import FailureReason, explain

    text = explain(FailureReason.TRUNCATED)
    # Mention max_tokens or "truncat" so the user can act on it.
    assert "max_tokens" in text or "trunc" in text.lower()
