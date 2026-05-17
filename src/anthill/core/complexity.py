"""Task complexity classification — let the system know when to slow down.

Before v0.8.1 the REPL ran every ask through the full pipeline:
Scout decomposes → execute → judge → maybe deliberate (loop). For
"hello" this is absurd — three LLM round trips for a one-word reply.

This module gives the orchestrator a way to ask "how hard is this
ask?" *before* spending LLM budget. The answer is one of three:

  trivial — single-word / short / no complex-task markers
            ⇒ skip Scout, skip deliberation, just route once
  normal  — typical user request
            ⇒ standard pipeline, deliberation only on demand
  complex — long, multi-clause, contains words like "research",
            "compare", "analyze", "write", etc.
            ⇒ deliberation default on

Two complementary signals:

  fast_classify(request) — pre-Scout regex/keyword heuristic. Fast (no
                           LLM call), conservative (returns None when
                           in doubt rather than guessing wrong).
                           Catches the obvious trivial cases so we
                           don't even spin up Scout for "hi".

  Scout-emitted hint    — when fast_classify returns None, Scout's own
                          plan JSON gains a `complexity` field. The
                          model sees the request and the work it
                          planned; it's well-positioned to label.
                          (Scout already runs for these cases — adding
                          one JSON key is free.)

The orchestrator (Nation.ask) picks the higher-fidelity signal:
fast_classify if confident, else Scout's emit. The downstream
deliberation policy honors it.
"""

from __future__ import annotations

import re
from typing import Literal


Complexity = Literal["trivial", "normal", "complex"]


# Markers that strongly suggest the user wants depth. Even a short
# request like "compare X and Y" should go through the full pipeline.
# Both English and Chinese — the project's two primary user languages.
_COMPLEX_MARKERS = (
    # English verbs implying multi-step work
    "research", "compare", "analyze", "analyse", "investigate",
    "summarize", "summarise", "synthesize", "synthesise",
    "translate", "review", "critique", "evaluate", "assess",
    "design", "implement", "build", "draft", "outline",
    "write a", "write an", "explain why", "explain how",
    "step by step", "in detail", "comprehensive", "thorough",
    # Chinese equivalents — common request verbs
    "调研", "研究", "比较", "对比", "分析", "评估",
    "总结", "归纳", "翻译", "评审", "审阅", "批评",
    "设计", "实现", "构建", "起草", "撰写", "写一",
    "解释为什么", "解释如何", "详细", "逐步", "全面",
    "深入", "深度",
)


# Words that, by themselves, mean a greeting or trivial ack. Used to
# fast-classify single-word inputs.
_TRIVIAL_LONE_WORDS = (
    "hi", "hello", "hey", "yo", "sup",
    "thanks", "thx", "ty", "ok", "okay",
    "bye", "goodbye", "cya",
    "你好", "您好", "谢谢", "再见", "嗨", "好的", "嗯",
)


def fast_classify(request: str) -> Complexity | None:
    """Pre-Scout heuristic. Returns None when not confident (let Scout decide).

    Order of checks matters:
      1. Complex markers present → 'complex'  (overrides everything)
      2. Question with multiple clauses (semicolons / sentence count) → 'complex'
      3. Single trivial word ('hi', '你好') → 'trivial'
      4. Very short input (<= 5 words) with no markers → 'trivial'
      5. Anything else → None (Scout decides)
    """
    text = request.strip()
    if not text:
        return "trivial"  # empty stays trivial — nothing to plan

    lower = text.lower()

    # 1. Complex-task markers win — even "research X" is complex despite being short.
    if _any_marker(lower):
        return "complex"

    # 2. Multi-clause / long-form text. Roughly: > 2 sentences or > 40 words.
    if _looks_long_form(text):
        return "complex"

    # 3. Single-word greetings / acks.
    words = text.split()
    if len(words) == 1 and words[0].lower().strip("!.?,") in _TRIVIAL_LONE_WORDS:
        return "trivial"

    # 4. Very short + no markers + no obvious question → trivial.
    if len(words) <= 5 and not _has_complex_punctuation(text):
        return "trivial"

    # 5. Ambiguous — Scout decides.
    return None


def _any_marker(lower_text: str) -> bool:
    return any(marker in lower_text for marker in _COMPLEX_MARKERS)


def _looks_long_form(text: str) -> bool:
    """Multi-sentence or long-paragraph input."""
    # Count sentence-terminal punctuation (.?!。？！) — > 2 means multi-step.
    terminators = sum(text.count(c) for c in ".?!。？！")
    if terminators > 2:
        return True
    # Word-count style: > 40 words is definitely complex even without
    # sentence breaks.
    if len(text.split()) > 40:
        return True
    return False


def _has_complex_punctuation(text: str) -> bool:
    """Semicolons, colons, or sentence chains hint at structure."""
    return bool(re.search(r"[;:。：；]", text))


def deliberation_default(complexity: Complexity) -> bool:
    """Policy: should we default to running the deliberation loop?

    Pulled into a function so the policy lives in one place and can be
    changed without grepping if/elses. Keeps mechanism (classification)
    separate from policy (what to do with it).
    """
    return complexity == "complex"


def description(complexity: Complexity) -> str:
    """One-line human-readable label, used in REPL display."""
    return {
        "trivial": "trivial · single-shot",
        "normal":  "normal · standard pipeline",
        "complex": "complex · deliberation enabled",
    }[complexity]


__all__ = [
    "Complexity",
    "fast_classify",
    "deliberation_default",
    "description",
]
