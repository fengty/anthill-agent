"""0.2.6 — anthill self-knowledge injection.

The bug fixed: "你如何和我的飞书对接的？" was treating "你" as a
generic AI assistant. Now requests like that flip a self-context
block into Scout's input.

The block's CONTENT (exact wording, channel list format, brevity
directive phrasing) is allowed to change. What needs a regression
test:
  - looks_self_referential discriminates self-asks from real asks
  - self_context_block includes configured channels (substance)
  - it doesn't crash on None config (defense)
  - self-referential detection survives conversation-context wrap
    suppression (the 0.2.8 contract)
"""

from __future__ import annotations

from dataclasses import dataclass

from anthill.core.self_context import (
    looks_self_referential,
    self_context_block,
)


@dataclass
class _StubChannelEntry:
    name: str
    kind: str


@dataclass
class _StubUserCfg:
    channels: list = None  # type: ignore[assignment]
    models: list = None    # type: ignore[assignment]


def test_self_referential_discrimination() -> None:
    """Self-asks fire; real-content asks don't. One assertion per
    category — if a new false positive shows up, add to the list."""
    self_asks = (
        "你能做什么",
        "你如何对接飞书",
        "anthill 是什么",
        "what can you do",
        "tell me about anthill",
    )
    for q in self_asks:
        assert looks_self_referential(q), f"missed: {q!r}"

    real_asks = (
        "翻译这段话",
        "分析这个 bug 的根因",
        "research the top 3 vector DBs",
        "",
    )
    for q in real_asks:
        assert not looks_self_referential(q), (
            f"false positive: {q!r}"
        )


def test_block_includes_configured_channels() -> None:
    """When channels are set up, they appear in the block — that's
    what makes 'how do you connect to lark?' answerable."""
    cfg = _StubUserCfg(
        channels=[_StubChannelEntry("larkbot", "lark")],
        models=[],
    )
    block = self_context_block(cfg)
    assert "larkbot" in block


def test_block_handles_no_channels_gracefully() -> None:
    """Fresh user, no channels yet → block must NOT pretend
    channels are connected. It should also tell them how to add
    one (concrete CLI command), not 'consult the docs'."""
    cfg = _StubUserCfg(channels=[], models=[])
    block = self_context_block(cfg)
    assert "anthill channel add" in block


def test_block_survives_none_config() -> None:
    """nation.ask may pass None when config load fails. Block must
    still produce something — not crash and not be empty."""
    block = self_context_block(None)
    assert len(block) > 50


def test_self_referential_marks_a_conversation_pivot() -> None:
    """0.2.8 contract: after a mysql conversation, asking about
    anthill itself should be detected so the REPL drops the
    conversation wrap. Without this, mysql context contaminates
    the answer."""
    from anthill.core.conversation import ConversationContext, is_follow_up

    c = ConversationContext()
    c.record("帮我部署 mysql 中间件", "用 RDS ...")
    ask = "你如何和我的飞书对接的？"
    # is_follow_up will say yes (short input + prior turn).
    assert is_follow_up(ask, c.last_turn()) is True
    # But the REPL uses looks_self_referential to ALSO know
    # this is a pivot, so the wrap is dropped.
    assert looks_self_referential(ask) is True
