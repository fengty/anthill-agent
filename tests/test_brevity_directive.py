"""0.2.9 — brevity directive in every citizen's system prompt.

Real-session evidence (sess-173c98b13a.jsonl) showed 30-second asks
that were 95% the model writing 8 KB tutorials nobody asked for.
This directive caps default verbosity. Citizens still go long when
the user explicitly says "详细" / "step by step" — but small-talk
gets small answers.
"""

from __future__ import annotations

from anthill.core.agent import Agent
from anthill.core.nation import _BREVITY_DIRECTIVE, Nation


def test_brevity_directive_is_non_empty() -> None:
    """The directive should be a real string (regression: an empty
    constant slipping in here means every citizen suddenly loses
    the cap)."""
    assert _BREVITY_DIRECTIVE.strip()
    assert len(_BREVITY_DIRECTIVE) > 100  # substantive instruction


def test_directive_mentions_concise_default() -> None:
    """The model needs to understand 'short by default'."""
    text = _BREVITY_DIRECTIVE.lower()
    assert "concise" in text or "brief" in text or "under" in text


def test_directive_lists_long_form_escape_hatch() -> None:
    """The directive must NOT make long-form impossible. Users
    sometimes genuinely want detailed output. Verify the escape
    hatches are documented in-prompt so the model knows them."""
    text = _BREVITY_DIRECTIVE
    # Some hatch must appear so the model knows when to go long.
    hatches = ["详细", "step by step", "tell me everything", "完整"]
    assert any(h in text for h in hatches), (
        f"directive missing long-form escape hatch from {hatches}"
    )


def test_directive_lands_at_top_of_system_prompt() -> None:
    """Composition order: brevity FIRST, then memory / persona /
    style. Later parts can override locally; the default has to be
    set first."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x", persona="be playful")]
    system = n._compose_system(n.agents[0])
    assert system is not None
    # First non-whitespace block is the brevity directive.
    assert system.lstrip().startswith(_BREVITY_DIRECTIVE.strip()[:60])


def test_directive_coexists_with_persona() -> None:
    """Adding the directive shouldn't drop persona / style / memory."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x", persona="speak like a poet")]
    n.memory_context = "MEMORY:\n- user prefers Chinese\n"
    system = n._compose_system(n.agents[0])
    assert system is not None
    assert "speak like a poet" in system
    assert "user prefers Chinese" in system


def test_directive_present_when_persona_empty() -> None:
    """A barebones nation (no persona, no memory, no style) still
    gets the brevity directive."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x")]
    n.memory_context = ""
    system = n._compose_system(n.agents[0])
    assert system is not None
    assert _BREVITY_DIRECTIVE.strip()[:60] in system


def test_directive_discourages_preamble() -> None:
    """The directive should explicitly call out the preamble
    pattern (好的，让我帮您...) that real sessions showed citizens
    writing before getting to actual content."""
    text = _BREVITY_DIRECTIVE
    # Some signal against ceremonial prelude.
    assert "preamble" in text.lower() or "好的" in text or "skip" in text.lower()


def test_directive_prefers_concrete_examples() -> None:
    """Real sessions had citizens writing prose before any concrete
    command. Directive should bias toward 'show the command first'."""
    text = _BREVITY_DIRECTIVE.lower()
    assert (
        "command" in text
        or "code" in text
        or "example" in text
        or "concrete" in text
    )
