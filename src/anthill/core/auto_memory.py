"""0.1.30 — auto-memory: detect "remember this" signals in user input.

0.1.29 gave us USER.md / MEMORY.md as durable plain-text files plus
the `/remember` and `/remember-me` slash commands. But asking the
user to type `/remember-me 我喜欢简洁回答` every time defeats the
"越用越聪明" promise. The agent should pick it up itself.

This module is the conservative rule-based first half of auto-memory.
The LLM-driven "decide what's durable" pass (mirroring Claude Code's
auto-memory v2.1.59 and Hermes's proactive save) comes later — this
patch only fires on EXPLICIT user declarations, where confidence is
near-certain.

Detection vocabulary:

- Direct memory imperatives — "记住 X" / "remember X" / "from now on"
  → USER.md preferences
- Self-description — "我是 X" / "I am a X" / "I work on X"
  → USER.md working style
- Direct preference — "我喜欢 X" / "I prefer X" / "I always X"
  → USER.md preferences
- Nation-fact — "this project uses X" / "the convention here is X"
  → MEMORY.md conventions (per-nation)

False-positive cost: a junk line in USER.md that the user notices and
deletes. False-negative cost: the system feels stupid. We bias toward
catching, but conservatively — markers must lead the sentence so
"... and I remember X happened" doesn't fire.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Where the signal goes when it fires.
TARGET_USER = "user"      # → ~/.anthill/USER.md
TARGET_NATION = "nation"  # → ~/.anthill/nations/<n>/MEMORY.md


@dataclass(frozen=True)
class MemorySignal:
    """One thing the auto-memory pass wants to remember."""

    target: str         # "user" or "nation"
    section: str        # markdown heading to file under, e.g. "Preferences"
    content: str        # the gist (already cleaned, ready to insert)


# Patterns: (regex, target, section). Order matters — earlier wins
# when multiple match. All match against the LOWERCASED stripped
# input; capture group 1 carries the gist.
#
# Each pattern requires the marker at sentence-start (anchored by ^
# or right after . / ; / 。 / ; in Chinese punctuation) so noise
# like "earlier you remembered X" doesn't trigger.
_LEADERS = r"(?:^|[.;。；]\s*)"

_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # --- direct memory imperatives ---
    (
        re.compile(_LEADERS + r"记住[，,：:\s]+(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    (
        re.compile(_LEADERS + r"请记住[，,：:\s]+(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    (
        re.compile(_LEADERS + r"remember\s+that\s+(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    (
        re.compile(_LEADERS + r"please\s+remember[:\s]+(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    (
        re.compile(_LEADERS + r"from\s+now\s+on[,\s]+(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    (
        re.compile(_LEADERS + r"以后[，,：:\s]*(?:都|总是|请)?\s*(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    # --- direct preference ---
    (
        re.compile(_LEADERS + r"我喜欢[，,：:\s]*(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    (
        re.compile(_LEADERS + r"我偏好[，,：:\s]*(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    (
        re.compile(_LEADERS + r"i\s+prefer\s+(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    (
        re.compile(_LEADERS + r"i\s+always\s+(.+)", re.IGNORECASE),
        TARGET_USER, "Preferences",
    ),
    # --- self-description ---
    (
        re.compile(_LEADERS + r"我是[一个]*\s*(.+)", re.IGNORECASE),
        TARGET_USER, "Working style",
    ),
    (
        re.compile(_LEADERS + r"i\s+am\s+(?:an?\s+)?(.+)", re.IGNORECASE),
        TARGET_USER, "Working style",
    ),
    (
        re.compile(_LEADERS + r"i\s+work\s+on\s+(.+)", re.IGNORECASE),
        TARGET_USER, "Working style",
    ),
    (
        re.compile(_LEADERS + r"我做的是[，,：:\s]*(.+)", re.IGNORECASE),
        TARGET_USER, "Working style",
    ),
    # --- nation-level facts (project conventions) ---
    (
        re.compile(_LEADERS + r"this\s+project\s+(?:uses|relies\s+on|requires)\s+(.+)", re.IGNORECASE),
        TARGET_NATION, "Conventions",
    ),
    (
        re.compile(_LEADERS + r"the\s+convention\s+here\s+is\s+(.+)", re.IGNORECASE),
        TARGET_NATION, "Conventions",
    ),
    (
        re.compile(_LEADERS + r"我们(?:这里|的项目|这个项目)?(?:用|使用|依赖)\s*(.+)", re.IGNORECASE),
        TARGET_NATION, "Conventions",
    ),
)


# Trailing punctuation to strip off the captured gist before writing.
_TRAILING_NOISE = ".,;!?。，；！？:：—-—_'\"`「」『』 \t"


def _clean_gist(raw: str) -> str:
    """Strip surrounding noise, cap length, drop trailing punctuation.

    Length cap protects USER.md from a rant getting auto-saved.
    Longer "remember this" content is better captured manually with
    `/remember-me` or just left in conversation history.
    """
    gist = raw.strip().rstrip(_TRAILING_NOISE).strip()
    if len(gist) > 200:
        gist = gist[:200].rstrip(_TRAILING_NOISE) + "…"
    return gist


def extract_memory_signals(request: str) -> list[MemorySignal]:
    """Find all explicit memory triggers in the user's input.

    Multiple matches are allowed (e.g. "我是设计师，我喜欢简洁" hits
    self-description + preference). The caller writes each one,
    so the user sees `📝 noted ×2` in the REPL.

    Returns [] for any input without explicit triggers — the caller
    skips the noted-line entirely.
    """
    text = request.strip()
    if not text:
        return []
    lower = text.lower()
    out: list[MemorySignal] = []
    seen: set[tuple[str, str]] = set()
    for pat, target, section in _PATTERNS:
        for match in pat.finditer(lower):
            # Re-extract from the original-case input so the saved
            # line preserves whatever capitalization the user typed.
            start = match.start(1)
            end = match.end(1)
            gist = _clean_gist(text[start:end])
            if not gist or len(gist) < 2:
                continue
            key = (target, gist.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                MemorySignal(target=target, section=section, content=gist)
            )
            # Stop scanning further occurrences with the SAME pattern
            # — protects against pathological inputs with the marker
            # repeated 20 times. Different patterns can still fire.
            break
    return out
