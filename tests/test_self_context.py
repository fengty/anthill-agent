"""0.2.6 — anthill self-knowledge injection tests.

The bug we're fixing: when the user asks "你能做什么" / "你如何接入飞书",
the citizen models had no idea "你" referred to anthill specifically.
They'd answer about AI assistants in general, or ask clarification
about WHICH AI to discuss. Real example:

  » 你如何和我的飞书对接的？
  · [1] clarify running...
    您是想了解：
    1. 如何将 AI 助手接入飞书机器人？
    2. 还是想将 MySQL 中间件的监控/告警等功能与飞书集成？

The model treated "你" as an abstract AI assistant. Self-knowledge
injection fixes this: when the request is self-referential, prepend
a compact identity block to Scout's context.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from anthill.core.self_context import (
    looks_self_referential,
    self_context_block,
)


# --- looks_self_referential ---------------------------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        # Chinese self-reference patterns
        "你能做什么",
        "你如何对接飞书",
        "你支持哪些 channel",
        "anthill 怎么用",
        "anthill 是什么",
        "蚁巢支持飞书吗",
        "怎么用你做定时任务",
        "你自己介绍一下",
        # English variants
        "what can you do",
        "how do I use this tool",
        "tell me about anthill",
        "this agent supports which channels",
        "yourself, what features",
    ],
)
def test_self_referential_requests_match(request_text: str) -> None:
    assert looks_self_referential(request_text), f"{request_text!r} missed"


@pytest.mark.parametrize(
    "request_text",
    [
        # Real-content requests — definitely not asking about anthill
        "翻译这段话到英文",
        "分析这个 bug 的根因",
        "summarize the standup notes",
        "research the top 3 vector DBs",
        "explain pheromone routing in one paragraph",
        "write me a haiku about ants",
        "",
        "   ",
    ],
)
def test_substantive_requests_dont_match(request_text: str) -> None:
    assert not looks_self_referential(request_text), (
        f"{request_text!r} incorrectly matched as self-referential"
    )


# --- self_context_block content ----------------------------------------


@dataclass
class _StubChannelEntry:
    name: str
    kind: str


@dataclass
class _StubUserCfg:
    channels: list = None  # type: ignore[assignment]
    models: list = None    # type: ignore[assignment]


def test_block_mentions_anthill_explicitly() -> None:
    """The whole point: models see 'you are anthill'."""
    block = self_context_block(None)
    assert "anthill" in block.lower()


def test_block_lists_configured_channels() -> None:
    cfg = _StubUserCfg(
        channels=[
            _StubChannelEntry(name="larkbot", kind="lark"),
            _StubChannelEntry(name="slacknotifier", kind="slack"),
        ],
        models=[],
    )
    block = self_context_block(cfg)
    assert "larkbot" in block
    assert "slacknotifier" in block
    # And the kind doc gets pulled in.
    assert "Lark" in block
    assert "Slack" in block


def test_block_handles_no_channels() -> None:
    """User hasn't set up any channels yet — say so, don't fake it."""
    cfg = _StubUserCfg(channels=[], models=[])
    block = self_context_block(cfg)
    assert "none configured" in block
    # And give them the hint command.
    assert "anthill channel add" in block


def test_block_handles_none_user_config() -> None:
    """Defensive: nation.ask may pass None when config load fails.
    Block still produces something useful."""
    block = self_context_block(None)
    assert "anthill" in block.lower()


def test_block_mentions_nation_name() -> None:
    """The block references the specific nation, not a generic one."""
    block = self_context_block(None, nation_name="custom-nation")
    assert "custom-nation" in block


def test_block_anchored_with_xml_marker() -> None:
    """The block uses <anthill_self> tags so models can cite without
    leaking the wrapper into their output."""
    block = self_context_block(None)
    assert "<anthill_self>" in block
    assert "</anthill_self>" in block


def test_block_lists_builtin_plugins() -> None:
    """A user asking 'what tools do you have' should see the plugin
    list when this block is in context."""
    block = self_context_block(None)
    for plugin in ("web_fetch", "file_read", "browser_render"):
        assert plugin in block


def test_block_shows_integration_commands() -> None:
    """The 'how do I integrate with X' answer should be concrete —
    actual CLI commands, not 'consult the docs'."""
    block = self_context_block(None)
    assert "anthill channel add" in block
    assert "anthill serve" in block
    assert "anthill cron" in block


def test_block_includes_anthill_version() -> None:
    """Useful when debugging — model knows which version it's in."""
    from anthill import __version__

    block = self_context_block(None)
    assert __version__ in block


def test_block_includes_brevity_directive() -> None:
    """0.2.7 update: the block should demand BRIEF answers, not
    elaborate tutorials. Previous version produced 8KB how-to walls
    for simple questions."""
    block = self_context_block(None)
    block_lower = block.lower()
    # Some variant of "be brief" / "stop" / "short answer" must
    # be present.
    assert (
        "brief" in block_lower
        or "concise" in block_lower
        or "short answer" in block_lower
        or "stop" in block_lower
    )


# --- integration sketch ------------------------------------------------


def test_self_referential_skips_follow_up_wrap() -> None:
    """0.2.8 — self-referential asks must NOT inherit conversation
    context. Otherwise after a few mysql turns, asking 'how do you
    connect to lark?' gets wrapped with mysql history and the model
    asks 'are you asking about mysql or anthill?'."""
    from anthill.core.conversation import ConversationContext, is_follow_up
    from anthill.core.self_context import looks_self_referential

    c = ConversationContext()
    # Pretend the user just discussed mysql middleware.
    c.record("帮我部署 mysql 中间件", "用阿里云 RDS for MySQL ...")

    # Now ask about anthill itself — clearly a new topic.
    ask = "你如何和我的飞书对接的？"
    last_turn = c.last_turn()
    # is_follow_up will fire (short input + prior turn).
    assert is_follow_up(ask, last_turn) is True
    # But the self-referential marker should ALSO fire — which is
    # what the REPL uses to suppress the wrap.
    assert looks_self_referential(ask) is True


def test_self_referential_request_full_pipeline_shape() -> None:
    """End-to-end shape: a self-referential request → block contains
    enough context to answer concretely.

    Doesn't run Scout / nation.ask — just verifies the helper produces
    text that's substantive enough (>200 chars). Smoke check for
    'did we accidentally strip the block down to nothing?'."""
    cfg = _StubUserCfg(
        channels=[_StubChannelEntry("larkbot", "lark")],
        models=[],
    )
    block = self_context_block(cfg)
    assert len(block) > 200
    # The user's question: "你如何对接飞书?" should now have a
    # concrete answer thanks to:
    assert "lark" in block.lower()
    assert "channel add" in block
