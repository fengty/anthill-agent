"""v0.9.0 — clarification turn tests.

Coverage:
  1. _parse_response handles the JSON contract + defensive fallbacks
  2. merge_answers folds user response into the original request
  3. assess_clarity catches failures (provider down, malformed) silently
  4. maybe_clarify orchestration:
     - no handler → no clarifier call
     - handler + clear → no change to request
     - handler + ambiguous + user answer → merged request
     - handler + ambiguous + user skip (None / empty) → unchanged
  5. Nation.ask integration: trivial requests skip clarifier entirely
"""

from __future__ import annotations

import pytest

from anthill.core.clarify import (
    ClarificationQuestions,
    _parse_response,
    assess_clarity,
    maybe_clarify,
    merge_answers,
)


# --- JSON parsing ----------------------------------------------------------


def test_parse_clear_true_returns_none() -> None:
    """clear=true ⇒ skip clarification."""
    assert _parse_response('{"clear": true, "questions": [], "why": ""}') is None


def test_parse_default_clear_is_true() -> None:
    """Missing `clear` defaults to True (skip — bias toward not annoying)."""
    assert _parse_response('{"questions": ["x?"], "why": "vague"}') is None


def test_parse_clear_false_returns_questions() -> None:
    out = _parse_response(
        '{"clear": false, "questions": ["who?", "what?"], "why": "vague"}'
    )
    assert isinstance(out, ClarificationQuestions)
    assert out.questions == ["who?", "what?"]
    assert out.why == "vague"


def test_parse_caps_questions_at_3() -> None:
    out = _parse_response(
        '{"clear": false, "questions": ["a", "b", "c", "d", "e"], "why": "x"}'
    )
    assert out is not None
    assert len(out.questions) == 3


def test_parse_filters_non_string_questions() -> None:
    out = _parse_response(
        '{"clear": false, "questions": ["valid", 42, null], "why": "x"}'
    )
    assert out is not None
    assert out.questions == ["valid"]


def test_parse_handles_code_fence() -> None:
    text = '```json\n{"clear": false, "questions": ["q?"], "why": "x"}\n```'
    out = _parse_response(text)
    assert out is not None
    assert out.questions == ["q?"]


def test_parse_extracts_embedded_json() -> None:
    text = 'Sure: {"clear": false, "questions": ["q?"], "why": "x"}. Hope this helps.'
    out = _parse_response(text)
    assert out is not None


def test_parse_garbage_returns_none() -> None:
    assert _parse_response("I'm sorry I can't help") is None
    assert _parse_response("") is None
    assert _parse_response("{not valid json") is None


def test_parse_returns_none_when_questions_empty() -> None:
    """clear=false but empty question list ⇒ treat as clear (defensive)."""
    assert _parse_response('{"clear": false, "questions": [], "why": "x"}') is None


def test_parse_non_object_returns_none() -> None:
    assert _parse_response('[1, 2, 3]') is None


def test_parse_provides_default_why_when_missing() -> None:
    out = _parse_response('{"clear": false, "questions": ["q?"]}')
    assert out is not None
    assert out.why  # non-empty fallback string


# --- merge_answers ---------------------------------------------------------


def test_merge_appends_user_response() -> None:
    merged = merge_answers("write something", "an email to my boss, short")
    assert "write something" in merged
    assert "an email to my boss, short" in merged
    assert "User's clarification" in merged


def test_merge_handles_empty_response() -> None:
    """Empty response = no clarification = return original."""
    assert merge_answers("hello", "") == "hello"
    assert merge_answers("hello", "   ") == "hello"


def test_merge_strips_whitespace() -> None:
    merged = merge_answers("  X  ", "  Y  ")
    assert "X" in merged
    assert "Y" in merged
    # Single trailing newline is fine; no rampant trailing whitespace.
    assert not merged.endswith(" \n")


# --- assess_clarity (with stub Nation) ------------------------------------


class _StubNation:
    """Minimal nation interface for clarifier tests."""

    def __init__(self, response_text: str = "", raises: Exception | None = None):
        self._response = response_text
        self._raises = raises
        self.run_called = 0
        self.last_task_type = None

    async def run(self, task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        self.run_called += 1
        self.last_task_type = task_type
        if self._raises:
            raise self._raises
        from anthill.core.agent import TaskResult
        return TaskResult(
            task_id="t", agent_id="ant-x", task_type=task_type,
            output=self._response, success_score=1.0, duration_seconds=0.0,
        )


@pytest.mark.asyncio
async def test_assess_clarity_uses_clarify_task_type() -> None:
    nation = _StubNation(response_text='{"clear": true, "questions": []}')
    await assess_clarity(nation, "any")  # type: ignore[arg-type]
    assert nation.last_task_type == "clarify"


@pytest.mark.asyncio
async def test_assess_clarity_returns_questions_when_ambiguous() -> None:
    nation = _StubNation(response_text=(
        '{"clear": false, "questions": ["which file?"], "why": "vague"}'
    ))
    out = await assess_clarity(nation, "summarize the file")  # type: ignore[arg-type]
    assert out is not None
    assert out.questions == ["which file?"]


@pytest.mark.asyncio
async def test_assess_clarity_returns_none_when_clear() -> None:
    nation = _StubNation(response_text='{"clear": true, "questions": []}')
    out = await assess_clarity(nation, "what is 2+2")  # type: ignore[arg-type]
    assert out is None


@pytest.mark.asyncio
async def test_assess_clarity_provider_failure_returns_none() -> None:
    """Clarifier blowing up should NOT block the user — silently skip."""
    nation = _StubNation(raises=RuntimeError("provider down"))
    out = await assess_clarity(nation, "anything")  # type: ignore[arg-type]
    assert out is None


@pytest.mark.asyncio
async def test_assess_clarity_empty_response_returns_none() -> None:
    nation = _StubNation(response_text="")
    out = await assess_clarity(nation, "anything")  # type: ignore[arg-type]
    assert out is None


# --- maybe_clarify orchestration -----------------------------------------


@pytest.mark.asyncio
async def test_maybe_clarify_no_handler_skips_clarifier() -> None:
    """No on_clarify ⇒ clarifier shouldn't even be invoked (cost saving)."""
    nation = _StubNation(response_text='{"clear": false, "questions": ["?"]}')
    result = await maybe_clarify(nation, "X", on_clarify=None)  # type: ignore[arg-type]
    assert result == "X"
    assert nation.run_called == 0


@pytest.mark.asyncio
async def test_maybe_clarify_clear_request_passes_through() -> None:
    nation = _StubNation(response_text='{"clear": true, "questions": []}')
    handler_called = False

    async def handler(_q):
        nonlocal handler_called
        handler_called = True
        return "answer"

    result = await maybe_clarify(nation, "what's 2+2", on_clarify=handler)  # type: ignore[arg-type]
    assert result == "what's 2+2"
    assert handler_called is False  # clarifier said clear; handler not invoked


@pytest.mark.asyncio
async def test_maybe_clarify_ambiguous_then_user_answers() -> None:
    nation = _StubNation(response_text=(
        '{"clear": false, "questions": ["what kind?"], "why": "vague"}'
    ))

    async def handler(q):
        return "an email"

    result = await maybe_clarify(nation, "write something", on_clarify=handler)  # type: ignore[arg-type]
    assert "write something" in result
    assert "an email" in result


@pytest.mark.asyncio
async def test_maybe_clarify_user_skips_returns_original() -> None:
    """User answering None / empty ⇒ proceed with original request."""
    nation = _StubNation(response_text=(
        '{"clear": false, "questions": ["what?"], "why": "x"}'
    ))

    async def skipping_handler(_q):
        return None

    result = await maybe_clarify(nation, "write something", on_clarify=skipping_handler)  # type: ignore[arg-type]
    assert result == "write something"


# --- Nation.ask integration ----------------------------------------------


@pytest.mark.asyncio
async def test_nation_ask_skips_clarifier_for_trivial_requests(monkeypatch) -> None:
    """fast_classify == trivial ⇒ no clarifier call (greetings never ambiguous)."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation

    clarifier_called = 0

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        nonlocal clarifier_called
        if task_type == "clarify":
            clarifier_called += 1
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="hi back", success_score=1.0, duration_seconds=0.0,
        )

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.run = fake_run  # type: ignore[assignment]

    async def handler(_q):
        return "answer"

    await n.ask("hi", on_clarify=handler)
    assert clarifier_called == 0, "trivial requests should not call the clarifier"


@pytest.mark.asyncio
async def test_nation_ask_invokes_clarifier_for_non_trivial(monkeypatch) -> None:
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan, Subtask as _Sub, Scout as _Scout

    clarifier_calls = []
    handler_called = False
    seen_request_to_scout: list[str] = []

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        if task_type == "clarify":
            clarifier_calls.append(prompt)
            return TaskResult(
                task_id="t", agent_id="ant-1", task_type=task_type,
                output='{"clear": false, "questions": ["what kind?"], "why": "vague"}',
                success_score=1.0, duration_seconds=0.0,
            )
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="result", success_score=1.0, duration_seconds=0.0,
        )

    async def fake_scout_plan(self, request, **kwargs):  # noqa: ANN001, ANN201, ARG002
        seen_request_to_scout.append(request)
        return _Plan(subtasks=[_Sub("general", "do it", [])])

    monkeypatch.setattr(_Scout, "plan", fake_scout_plan)

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.run = fake_run  # type: ignore[assignment]

    async def handler(_q):
        nonlocal handler_called
        handler_called = True
        return "an email to my boss"

    # "write something for me" is 4 words → fast_classify catches as trivial,
    # which skips clarification by design. Use a request long enough to fall
    # through to "normal" (clarifier should fire on ambiguous normal).
    await n.ask(
        "help me figure out a presentation thing for next week",
        on_clarify=handler,
    )

    assert len(clarifier_calls) == 1
    assert handler_called
    # Scout should have seen the MERGED request (with user's clarification)
    assert any("an email to my boss" in r for r in seen_request_to_scout)


@pytest.mark.asyncio
async def test_nation_ask_no_handler_means_no_clarifier_call(monkeypatch) -> None:
    """Even non-trivial requests skip clarifier when no on_clarify is given."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan as _Plan, Subtask as _Sub, Scout as _Scout

    clarifier_calls = 0

    async def fake_run(task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
        nonlocal clarifier_calls
        if task_type == "clarify":
            clarifier_calls += 1
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="result", success_score=1.0, duration_seconds=0.0,
        )

    async def fake_scout_plan(self, request, **kwargs):  # noqa: ANN001, ANN201, ARG002
        return _Plan(subtasks=[_Sub("general", "do it", [])])

    monkeypatch.setattr(_Scout, "plan", fake_scout_plan)

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.run = fake_run  # type: ignore[assignment]

    # Same non-trivial request shape as the previous test — eliminates
    # ambiguity over whether trivial fast-path was the reason.
    await n.ask("help me figure out a presentation thing for next week")
    assert clarifier_calls == 0
