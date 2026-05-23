"""0.2.9 — brevity directive in every citizen's system prompt.

Real-session evidence (sess-173c98b13a.jsonl) showed 30-second
asks that were 95% the model writing 8 KB tutorials nobody asked
for. This directive caps default verbosity.

The directive's WORDING is allowed to change. What tests need to
catch is:
  - It's actually applied (not silently dropped from _compose_system)
  - It composes with persona / memory / style without dropping them
  - It comes BEFORE later parts (length cap is the floor others
    layer on top of)
"""

from __future__ import annotations

from anthill.core.agent import Agent
from anthill.core.nation import _BREVITY_DIRECTIVE, Nation


def test_directive_applied_with_persona_and_memory() -> None:
    """The directive lands in the composed system prompt and
    doesn't crowd out persona / memory_context."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x", persona="speak like a poet")]
    n.memory_context = "MEMORY:\n- user prefers Chinese\n"
    system = n._compose_system(n.agents[0])
    assert system is not None
    # Brevity content present.
    assert _BREVITY_DIRECTIVE.strip()[:60] in system
    # Persona + memory survived composition.
    assert "speak like a poet" in system
    assert "user prefers Chinese" in system


def test_brevity_lands_before_persona_and_style() -> None:
    """0.2.27 changed order: AGENT_IDENTITY_PREAMBLE comes first
    (identity overrides chatbot defaults), then brevity, THEN
    persona / memory / style. The relative order of brevity vs.
    those later sections is what matters — brevity sets length
    defaults that later sections can override locally."""
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="x", persona="be playful")]
    system = n._compose_system(n.agents[0]) or ""
    brevity_pos = system.find(_BREVITY_DIRECTIVE.strip()[:40])
    persona_pos = system.find("be playful")
    assert brevity_pos >= 0, "brevity directive missing from prompt"
    assert persona_pos >= 0, "persona missing from prompt"
    assert brevity_pos < persona_pos, "brevity should come BEFORE persona"
