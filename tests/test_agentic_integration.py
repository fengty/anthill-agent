"""0.2.30 — Agent loop integration into Nation.run / Agent.execute.

This is the version where 0.2.29's foundation gets wired to the
actual citizen execution path.

Tests cover:
  - nation.agentic_mode=False → unchanged behavior (single-shot)
  - nation.agentic_mode=True with a real native-tool_use provider →
    loop fires, tool calls execute, final text returned
  - exec_disabled supersedes agentic_mode
  - browser_executor reaches BrowserSession via the dispatch
  - Anthropic message translation round-trip (OpenAI → Anthropic
    content blocks → response back to OpenAI shape)
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.tool_executors import browser_executor
from anthill.core.tools_protocol import ToolCall, ToolResult
from anthill.models.base import ModelProvider, ModelResponse


class _NativeProvider(ModelProvider):
    """A provider that DOES implement complete_with_messages with
    canned responses — mimics deepseek's tool_use behavior."""

    name = "native-fake"

    def __init__(self, responses: list[ModelResponse]):
        self._responses = list(responses)

    async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
        # The agent_loop only calls complete_with_messages, but this
        # has to exist (abstract method).
        return ModelResponse(text="(unused)", model=self.name)

    async def complete_with_messages(
        self, messages, *, system=None, tools=None,
        max_tokens=4096, temperature=0.7,
    ):
        if not self._responses:
            return ModelResponse(text="(no more)", model=self.name)
        return self._responses.pop(0)


def _r(text: str, *, tool_calls=None, in_tok=10, out_tok=5):
    from anthill.models.base import ModelResponse
    return ModelResponse(
        text=text, model="native-fake",
        input_tokens=in_tok, output_tokens=out_tok,
        finish_reason="stop", tool_calls=tool_calls or [],
    )


def _tc(call_id, name, args):
    return ToolCall(id=call_id, name=name, arguments=args)


def _nation_with_native(provider) -> Nation:
    n = Nation(name="t")
    a = Agent(id="ant-1", model="native-fake")
    a._provider = provider  # type: ignore[assignment]
    n.agents = [a]
    return n


# --- agentic_mode gating --------------------------------------------


def test_agentic_off_uses_single_shot() -> None:
    """Default behavior: nation.run goes through provider.complete,
    not the agent loop. Token counts come from the single call."""
    provider = _NativeProvider([])  # would be empty if loop fired

    class _Echo(ModelProvider):
        name = "echo"
        async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
            return ModelResponse(text=f"echo: {prompt}", model="echo", input_tokens=7, output_tokens=3)
        # NO complete_with_messages override → falls back to ABC default

    n = _nation_with_native(_Echo())
    n.agentic_mode = False  # explicit
    result = asyncio.run(n.run("general", "hi"))
    assert "echo: hi" in result.output
    assert result.input_tokens == 7


def test_agentic_on_uses_loop_when_provider_supports() -> None:
    """When agentic_mode is on AND provider has native
    complete_with_messages, the loop runs."""
    provider = _NativeProvider([
        _r("ok let me check", tool_calls=[_tc("c1", "bash_run", {"cmd": "echo hi"})]),
        _r("found 'hi'"),
    ])
    n = _nation_with_native(provider)
    n.agentic_mode = True

    result = asyncio.run(n.run("research", "what does echo say"))
    assert result.output == "found 'hi'"
    # Tokens accumulated from BOTH iterations.
    assert result.input_tokens == 20  # 10 + 10
    assert result.output_tokens == 10  # 5 + 5


def test_agentic_on_falls_back_when_provider_lacks_native() -> None:
    """Provider without complete_with_messages override → loop SKIPS
    even with agentic_mode on. Keeps test fakes working."""
    class _Plain(ModelProvider):
        name = "plain"
        async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
            return ModelResponse(text="plain reply", model="plain", input_tokens=4, output_tokens=2)

    n = _nation_with_native(_Plain())
    n.agentic_mode = True  # on, but provider can't honor

    result = asyncio.run(n.run("general", "hi"))
    # Single-shot path ran.
    assert result.output == "plain reply"


def test_noexec_supersedes_agentic_on() -> None:
    """/noexec means 'no shell/browser'. agentic_mode is meaningless
    without tools; the loop must NOT fire."""
    provider = _NativeProvider([
        _r("would call tool", tool_calls=[_tc("c1", "bash_run", {"cmd": "x"})]),
        _r("done"),
    ])
    n = _nation_with_native(provider)
    n.agentic_mode = True
    n._exec_disabled = True  # type: ignore[attr-defined]

    result = asyncio.run(n.run("general", "hi"))
    # Loop should NOT have run; we fall through to single-shot
    # path which calls .complete() (returns "(unused)" from our fake).
    assert "would call tool" not in result.output


# --- Anthropic message translation ----------------------------------


def test_anthropic_translation_round_trip(monkeypatch) -> None:
    """OpenAI-shape messages going INTO _anthropic_with_tools come
    out as Anthropic content-block format in the HTTP payload. We
    don't need a real Anthropic API — just verify the translation
    by intercepting the httpx call."""
    from anthill.models.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key="test", model="claude-3-5-sonnet",
        base_url="https://api.anthropic.com/v1",
        provider_name="anthropic",
    )

    sent_payload = {}

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent_payload.update(json or {})
            class _R:
                def raise_for_status(self): pass
                def json(self):
                    return {
                        "content": [{"type": "text", "text": "from claude"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    }
            return _R()

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

    # Messages include a prior assistant tool_call + tool result:
    messages = [
        {"role": "user", "content": "check ports"},
        {
            "role": "assistant",
            "content": "let me check",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "bash_run", "arguments": '{"cmd": "lsof"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "PID 1234"},
    ]
    asyncio.run(provider.complete_with_messages(
        messages, system="you are helpful",
    ))

    # System lifted to top-level field.
    assert sent_payload["system"] == "you are helpful"
    # Assistant tool_calls → tool_use content blocks.
    assistant_msg = sent_payload["messages"][1]
    assert assistant_msg["role"] == "assistant"
    blocks = assistant_msg["content"]
    # text block + tool_use block.
    types = [b.get("type") for b in blocks]
    assert "text" in types
    assert "tool_use" in types
    # Tool message → user message with tool_result block.
    tool_msg = sent_payload["messages"][2]
    assert tool_msg["role"] == "user"
    assert tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][0]["tool_use_id"] == "c1"


def test_anthropic_response_parses_tool_use_blocks(monkeypatch) -> None:
    """A response with tool_use blocks becomes ModelResponse.tool_calls."""
    from anthill.models.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key="test", model="claude-3-5-sonnet",
        base_url="https://api.anthropic.com/v1",
        provider_name="anthropic",
    )

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            class _R:
                def raise_for_status(self): pass
                def json(self):
                    return {
                        "content": [
                            {"type": "text", "text": "let me check that"},
                            {
                                "type": "tool_use",
                                "id": "toolu_abc",
                                "name": "bash_run",
                                "input": {"cmd": "ps aux"},
                            },
                        ],
                        "stop_reason": "tool_use",
                        "usage": {"input_tokens": 50, "output_tokens": 20},
                    }
            return _R()

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

    resp = asyncio.run(provider.complete_with_messages(
        [{"role": "user", "content": "what's running"}],
    ))
    assert resp.text == "let me check that"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "toolu_abc"
    assert resp.tool_calls[0].name == "bash_run"
    assert resp.tool_calls[0].arguments == {"cmd": "ps aux"}
    assert resp.finish_reason == "tool_use"


# --- browser_executor wired -------------------------------------------


def test_browser_executor_returns_error_when_no_action() -> None:
    """Defensive: missing 'action' kwarg → structured error, not crash."""
    result = asyncio.run(browser_executor(
        ToolCall(id="t1", name="browser_action", arguments={})
    ))
    assert result.is_error
    assert "action" in result.content.lower()
