"""0.2.28 — hermes-style tool-use enforcement.

Learned from /Users/fty/Desktop/code/ai/fun/hermes-agent/agent/
prompt_builder.py (TOOL_USE_ENFORCEMENT_GUIDANCE +
OPENAI_MODEL_EXECUTION_GUIDANCE):

  - XML-tagged sections in identity preamble (each rule parses
    independently in the model's attention)
  - Mandatory_tool_use category list (arithmetic / time / system
    state / file contents / git → always tool)
  - Model-family routing: gpt/gemini/glm/deepseek-ish models get
    enforcement repeated near END of prompt (recency bias)

Tests cover the routing + the structure of the preamble.
"""

from __future__ import annotations

from anthill.core.agent import Agent
from anthill.core.nation import Nation
from anthill.core.shell import (
    AGENT_IDENTITY_PREAMBLE,
    TOOL_USE_REINFORCEMENT_TAIL,
    model_needs_strong_tool_reinforcement,
)


# --- preamble structure ---------------------------------------------


def test_preamble_has_mandatory_xml_tagged_sections() -> None:
    """The XML tags help models parse the rules independently —
    if we drop them and write prose, instruction-following drops."""
    required_tags = (
        "<tool_use_enforcement>", "</tool_use_enforcement>",
        "<mandatory_tool_use>", "</mandatory_tool_use>",
        "<act_dont_ask>", "</act_dont_ask>",
        "<forbidden_phrases>", "</forbidden_phrases>",
        "<worked_examples>", "</worked_examples>",
    )
    for tag in required_tags:
        assert tag in AGENT_IDENTITY_PREAMBLE, f"missing tag {tag!r}"


def test_preamble_covers_must_tool_categories() -> None:
    """The mandatory_tool_use section enumerates the situations
    where the model MUST use a tool. Each category here was a real
    failure mode in production logs or hermes' battle wisdom."""
    categories = (
        # System state queries
        "ports",
        "processes",
        # Time / current facts
        "time",
        # File operations
        "file",
        # Git
        "git",
    )
    p = AGENT_IDENTITY_PREAMBLE.lower()
    for cat in categories:
        assert cat in p, f"mandatory_tool_use missing category: {cat!r}"


# --- model-family routing -------------------------------------------


def test_model_family_routing_catches_deepseek() -> None:
    """Real user failure: deepseek emitted '我没有执行系统命令的
    能力'. That family needs the trailing reinforcement."""
    assert model_needs_strong_tool_reinforcement("deepseek") is True
    assert model_needs_strong_tool_reinforcement("deepseek-chat") is True
    assert model_needs_strong_tool_reinforcement("deepseek-v3") is True


def test_model_family_routing_catches_other_chatbot_priors() -> None:
    """Models that share deepseek's failure pattern: glm (Zhipu),
    minimax, qwen, gpt (training has strong assistant prior),
    gemini family. All need reinforcement."""
    families = ("glm-4", "minimax-abab", "qwen2.5", "gpt-4o", "gemini-1.5", "gemma-2")
    for fam in families:
        assert model_needs_strong_tool_reinforcement(fam) is True, (
            f"{fam} should need reinforcement"
        )


def test_model_family_routing_skips_claude() -> None:
    """Claude (Sonnet / Opus / Haiku) follows the front-loaded
    preamble well — doesn't need tail reinforcement. Adding it
    would just waste prompt budget."""
    for model in ("claude-3-5-sonnet", "claude-opus-4", "claude-haiku"):
        assert model_needs_strong_tool_reinforcement(model) is False, (
            f"{model} doesn't need tail reinforcement"
        )


def test_model_family_routing_handles_empty_string() -> None:
    """Defensive: empty model name shouldn't crash."""
    assert model_needs_strong_tool_reinforcement("") is False


# --- composition: tail appears when model needs it ------------------


def test_tail_appended_for_deepseek_citizen() -> None:
    """When the citizen's model is deepseek, the trailing
    reinforcement IS in the composed system prompt."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="deepseek-chat")]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    # The distinctive line from the tail.
    assert "REMEMBER" in sys_prompt
    # And it lands AT THE END (after the front-loaded preamble).
    front = sys_prompt.find("WHAT YOU ARE")
    tail = sys_prompt.find("REMEMBER")
    assert front < tail, "tail reinforcement should come AFTER the preamble"


def test_tail_skipped_for_claude_citizen() -> None:
    """Claude doesn't get the tail — it follows the front-loaded
    instructions without reinforcement."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="claude-3-5-sonnet")]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    # The distinctive line should NOT be present.
    assert "REMEMBER (this is the last thing" not in sys_prompt


def test_tail_skipped_when_exec_disabled() -> None:
    """If /noexec is on, the model has no tools — claiming 'use
    your tools' in the tail would be a lie. Skip it."""
    n = Nation(name="t")
    n.agents = [Agent(id="a", model="deepseek-chat")]
    n._exec_disabled = True  # type: ignore[attr-defined]
    sys_prompt = n._compose_system(n.agents[0]) or ""
    assert "REMEMBER (this is the last thing" not in sys_prompt
