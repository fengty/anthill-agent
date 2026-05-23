"""0.2.27 — anthill is an agent, not a chatbot.

User feedback (real session screenshot): asking "find PID on port
8190" produced:
    "我没有执行系统命令的能力，无法访问你的本地机器"
    followed by a markdown ```bash`` code fence tutorial.

That contradicts our SHELL_TOOL_INSTRUCTION. Fix is two-layered:

  1. PROACTIVE — AGENT_IDENTITY_PREAMBLE at the absolute top of
     every system prompt, banning the denial phrases explicitly.
  2. REACTIVE — detect_denial() catches the failure when it
     happens anyway; the REPL surfaces a strong "citizen 违反契约"
     message + queues the suggested command + erodes pheromone.

Tests cover both layers.
"""

from __future__ import annotations

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.shell import (
    AGENT_IDENTITY_PREAMBLE,
    detect_denial,
)


# --- proactive: identity preamble in system prompt --------------------


def test_identity_preamble_in_default_system_prompt() -> None:
    """Every citizen sees the identity preamble when exec is on."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    # Preamble's distinctive phrase.
    assert "citizen-agent" in sys_prompt or "running INSIDE a real shell" in sys_prompt


def test_identity_preamble_first_in_prompt() -> None:
    """Order matters: identity BEFORE brevity. The model should see
    'you are an agent' before 'be concise' so brevity doesn't
    accidentally feel like 'just describe briefly'."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    # Identity preamble appears before brevity directive.
    identity_pos = sys_prompt.find("citizen-agent")
    brevity_pos = sys_prompt.find("concise")
    if brevity_pos >= 0 and identity_pos >= 0:
        assert identity_pos < brevity_pos


def test_identity_preamble_dropped_when_noexec() -> None:
    """/noexec means no shell/browser. Claiming 'you can act' under
    those conditions would be a lie — drop the preamble."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="x")]
    n._exec_disabled = True  # type: ignore[attr-defined]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    assert "citizen-agent" not in sys_prompt


def test_identity_preamble_lists_banned_phrases() -> None:
    """The preamble must spell out the exact phrases to never emit.
    A model that's only told 'be an agent' would still slip into
    its trained denial language; explicit banlist helps it
    self-check."""
    assert "我没有执行系统命令" in AGENT_IDENTITY_PREAMBLE
    assert "I don't have shell access" in AGENT_IDENTITY_PREAMBLE
    assert "我无法访问你的本地机器" in AGENT_IDENTITY_PREAMBLE


# --- reactive: denial detection --------------------------------------


def test_denial_caught_chinese_variants() -> None:
    """The real failure mode in the user's screenshot."""
    txt = "我没有执行系统命令的能力，无法访问你的本地机器或网络。"
    assert detect_denial(txt) is not None


def test_denial_caught_english_variants() -> None:
    """Same failure in English."""
    txt = "I don't have shell access, you'll need to run this in your terminal."
    assert detect_denial(txt) is not None


def test_denial_caught_chatbot_self_label() -> None:
    """'I am just a chatbot' — common deepseek/minimax pattern."""
    txt = "Sorry, I'm just an AI assistant, you can run: lsof -i :8190"
    assert detect_denial(txt) is not None


def test_normal_response_not_flagged_as_denial() -> None:
    """Responses that include [[bash:]] and actual work are NOT
    denial — even if they contain words like 'shell' or 'command'."""
    normal_responses = (
        "Let me check: [[bash:lsof -i :8190]]",
        "I ran ping and got 0% loss.",
        "The shell command completed successfully.",
        "",
    )
    for t in normal_responses:
        assert detect_denial(t) is None, f"false positive: {t!r}"


def test_partial_match_within_longer_text() -> None:
    """The denial phrase might be buried mid-paragraph. We catch it
    by substring; the model often writes 'unfortunately, 我没有
    执行系统命令的能力, so here is what you can do:'"""
    txt = (
        "好的, 不过我没有执行系统命令的能力. 你可以用这些命令:\n"
        "```bash\nlsof -i :8190\n```"
    )
    assert detect_denial(txt) is not None
