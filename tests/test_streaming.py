"""0.1.10 — provider streaming + token ProgressEvents.

Closes the "is it hung?" feeling during long deliberations. The
provider layer gains a ``stream()`` method (with a default fallback
to ``complete()``); Agent.execute uses it when an ``on_token``
callback is set; the executor bridges deltas into
``ProgressEvent(kind='token')`` so the REPL renders inline.

Tests cover:
  1. ModelProvider default stream() falls back to complete()
  2. OpenAI-shape SSE parsing: delta extraction, usage harvest, [DONE]
  3. Anthropic-shape SSE parsing: content_block_delta + message_stop
  4. _parse_sse_line handles edge cases (comments, malformed JSON)
  5. Agent.execute streams when on_token set; accumulates correctly
  6. Agent.execute without on_token still calls complete()
  7. Executor emits ProgressEvent(kind='token') with deltas
  8. ProgressEvent default delta is empty for non-token events
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest


# ---------------------------------------------------------------------------
# Provider base + SSE parsing
# ---------------------------------------------------------------------------


def test_streamchunk_default_done_false() -> None:
    from anthill.models.base import StreamChunk

    c = StreamChunk()
    assert c.delta == ""
    assert c.done is False
    assert c.input_tokens == 0
    assert c.output_tokens == 0


@pytest.mark.asyncio
async def test_default_stream_falls_back_to_complete() -> None:
    """A provider that only implements complete() still streams (one chunk)."""
    from anthill.models.base import ModelProvider, ModelResponse

    class StubProvider(ModelProvider):
        name = "stub"

        async def complete(self, prompt, *, system=None, max_tokens=1024, temperature=0.7):
            return ModelResponse(
                text="hello world",
                model="stub-1",
                input_tokens=5,
                output_tokens=2,
            )

    chunks = []
    async for chunk in StubProvider().stream("anything"):
        chunks.append(chunk)
    assert len(chunks) == 1
    assert chunks[0].delta == "hello world"
    assert chunks[0].done is True
    assert chunks[0].output_tokens == 2


def test_parse_sse_line_handles_edge_cases() -> None:
    from anthill.models.openai_compatible import _parse_sse_line

    assert _parse_sse_line("") is None
    assert _parse_sse_line(": this is a comment") is None
    assert _parse_sse_line("event: ping") is None
    assert _parse_sse_line("data: ") is None
    assert _parse_sse_line("data: [DONE]") == "[DONE]"
    assert _parse_sse_line('data: {"a": 1}') == {"a": 1}
    assert _parse_sse_line("data: not-json-{{") is None


# ---------------------------------------------------------------------------
# OpenAI-compatible provider streaming (mocked httpx)
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal stand-in for httpx streaming response in tests."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


class _FakeStreamClient:
    """Mocks httpx.AsyncClient with a programmable .stream() response."""

    response_lines: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def stream(self, method, url, json=None, headers=None):
        lines = self.response_lines

        class _Ctx:
            async def __aenter__(self_inner):
                return _FakeStreamResponse(lines)

            async def __aexit__(self_inner, *args):
                pass

        return _Ctx()


@pytest.mark.asyncio
async def test_openai_stream_parses_deltas_and_usage(monkeypatch) -> None:
    """OpenAI-shape SSE: deltas accumulate; final [DONE] carries usage."""
    import httpx

    from anthill.models.openai_compatible import OpenAICompatibleProvider

    _FakeStreamClient.response_lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo "}}]}',
        'data: {"choices":[{"delta":{"content":"world"}}]}',
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":4,"completion_tokens":3}}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeStreamClient)

    p = OpenAICompatibleProvider(
        api_key="k",
        model="m",
        base_url="https://api.test/v1",
        provider_name="openai",
    )
    chunks = []
    async for chunk in p.stream("hi"):
        chunks.append(chunk)
    # Three deltas + one terminal chunk.
    deltas = [c.delta for c in chunks if c.delta]
    assert "".join(deltas) == "Hello world"
    assert chunks[-1].done is True
    assert chunks[-1].input_tokens == 4
    assert chunks[-1].output_tokens == 3


@pytest.mark.asyncio
async def test_anthropic_stream_parses_content_block_delta(monkeypatch) -> None:
    """Anthropic-shape SSE: content_block_delta events become token chunks."""
    import httpx

    from anthill.models.openai_compatible import OpenAICompatibleProvider

    _FakeStreamClient.response_lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}',
        'data: {"type":"content_block_start","index":0}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" there"}}',
        'data: {"type":"message_delta","usage":{"output_tokens":5}}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeStreamClient)

    p = OpenAICompatibleProvider(
        api_key="k",
        model="claude-x",
        base_url="https://api.anthropic.test/v1",
        provider_name="anthropic",
    )
    chunks = []
    async for chunk in p.stream("hi"):
        chunks.append(chunk)
    deltas = [c.delta for c in chunks if c.delta]
    assert "".join(deltas) == "Hi there"
    terminal = chunks[-1]
    assert terminal.done is True
    assert terminal.input_tokens == 7
    assert terminal.output_tokens == 5


# ---------------------------------------------------------------------------
# Agent.execute streaming path
# ---------------------------------------------------------------------------


class _ScriptedStreamProvider:
    """Yields a pre-canned sequence of chunks for the Agent test."""

    def __init__(self, deltas: list[str]) -> None:
        self.deltas = deltas
        self.name = "scripted"
        self.complete_called = 0

    async def complete(self, prompt, *, system=None, max_tokens=1024, temperature=0.7):
        from anthill.models.base import ModelResponse

        self.complete_called += 1
        return ModelResponse(text="non-stream", model="scripted", output_tokens=99)

    async def stream(self, prompt, *, system=None, max_tokens=1024, temperature=0.7):
        from anthill.models.base import StreamChunk

        for d in self.deltas:
            yield StreamChunk(delta=d)
        yield StreamChunk(done=True, input_tokens=3, output_tokens=len(self.deltas))


@pytest.mark.asyncio
async def test_agent_execute_streams_when_on_token_set() -> None:
    from anthill.core.agent import Agent

    a = Agent(id="ant-1", model="scripted")
    provider = _ScriptedStreamProvider(["foo", "bar", "baz"])
    a._provider = provider

    received: list[str] = []

    async def on_token(delta: str) -> None:
        received.append(delta)

    result = await a.execute("general", "do it", on_token=on_token)
    assert received == ["foo", "bar", "baz"]
    assert result.output == "foobarbaz"
    assert result.output_tokens == 3
    assert provider.complete_called == 0  # streaming path bypassed complete()


@pytest.mark.asyncio
async def test_agent_execute_no_callback_uses_complete() -> None:
    """on_token=None preserves the non-streaming behavior exactly."""
    from anthill.core.agent import Agent

    a = Agent(id="ant-2", model="scripted")
    provider = _ScriptedStreamProvider(["should-not-stream"])
    a._provider = provider

    result = await a.execute("general", "do it")
    assert result.output == "non-stream"
    assert provider.complete_called == 1


# ---------------------------------------------------------------------------
# Executor → ProgressEvent(kind='token') bridge
# ---------------------------------------------------------------------------


def test_progress_event_default_delta_empty() -> None:
    """Existing event kinds keep their default delta="" so callers don't break."""
    from anthill.core.executor import ProgressEvent, SubtaskOutcome
    from anthill.core.scout import Subtask

    st = Subtask(task_type="general", prompt="x", depends_on=[])
    ev = ProgressEvent(
        kind="started", index=0, subtask=st, outcome=SubtaskOutcome(subtask=st)
    )
    assert ev.delta == ""


@pytest.mark.asyncio
async def test_executor_emits_token_events_for_streaming_subtasks() -> None:
    """When a subtask runs serially (fanout=1), token deltas become events."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.executor import execute_plan
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan, Subtask

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="scripted")]

    async def fake_run(task_type, prompt, *, forbid=None, on_token=None):
        # Simulate a provider that emits 3 deltas before returning.
        if on_token is not None:
            for d in ["alpha ", "beta ", "gamma"]:
                await on_token(d)
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="alpha beta gamma",
            success_score=1.0,
            duration_seconds=0.0,
        )

    n.run = fake_run  # type: ignore[assignment]

    plan = Plan(subtasks=[Subtask("general", "do it", [])])
    events = []

    async def on_progress(ev):
        events.append(ev)

    await execute_plan(plan, n, on_progress=on_progress)

    token_events = [e for e in events if e.kind == "token"]
    assert [e.delta for e in token_events] == ["alpha ", "beta ", "gamma"]
    for ev in token_events:
        assert ev.attempt_number == 1
        assert ev.index == 0


@pytest.mark.asyncio
async def test_executor_no_token_events_when_no_progress_callback() -> None:
    """No progress callback ⇒ no streaming overhead pushed to providers."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.executor import execute_plan
    from anthill.core.nation import Nation
    from anthill.core.scout import Plan, Subtask

    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="scripted")]

    seen_on_token = []

    async def fake_run(task_type, prompt, *, forbid=None, on_token=None):
        seen_on_token.append(on_token)
        return TaskResult(
            task_id="t",
            agent_id="ant-1",
            task_type=task_type,
            output="ok",
            success_score=1.0,
            duration_seconds=0.0,
        )

    n.run = fake_run  # type: ignore[assignment]

    plan = Plan(subtasks=[Subtask("general", "do it", [])])
    await execute_plan(plan, n)
    # Without on_progress, nation.run should receive on_token=None so
    # the agent stays on its non-streaming complete() path.
    assert seen_on_token == [None]
