"""0.1.28 — in-session conversation memory.

The bug a real user hit twice in one screenshot:

  » 最近热门电影
  ✓ (answer about 2024-era movies, hallucinated)

  » 我说的是 2026 年的
  ✓ (asks user "什么 2026 年的话题？" — totally lost context)

Anthill's history layer (``core/history.py``) records every ask
hash-chained for the long term, and the episodic search
(``core/episodic.py``) surfaces similar PAST asks to Scout. Neither
of those is the "what did the user just say one turn ago"
conversation memory. So follow-ups arrived at Scout as fresh asks.

This module is the missing piece. Tiny on purpose:

- ``ConversationContext`` — rolling list of (request, final_output)
  tuples, capped at ``maxlen`` so context windows don't blow up.
- ``is_follow_up(current, prior)`` — cheap pattern-match heuristic
  (keywords from the complexity module + length/reference cues).
- ``wrap_with_context(current, history, *, max_chars)`` — produces
  the prompt that actually reaches Scout. The previous Q+A is
  inlined under a header the planner understands.

The REPL owns the lifetime: one ``ConversationContext`` per session,
cleared on ``/clear`` or ``/nation`` switch.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable


# How many recent turns to keep in the rolling window.
# 4 covers the common "follow-up → follow-up → follow-up" thread
# without bloating Scout's prompt past the 8K mark for typical use.
DEFAULT_MAXLEN = 4

# Per-turn content cap when wrapping for Scout. The full original
# answers can be long; we summarize at the head and tail so the
# planner has anchors without paying for the whole essay.
DEFAULT_TURN_CHARS = 1500

# Reuse the same vocabulary AND the same matcher the complexity
# classifier uses. Single source of truth so the two paths agree.
from anthill.core.complexity import has_follow_up_marker  # noqa: E402


@dataclass
class Turn:
    """One observed conversation step."""

    request: str
    response: str
    timestamp: float = 0.0  # seconds since epoch; 0 if not set


class ConversationContext:
    """Rolling in-session conversation window. Bounded, side-effect-free.

    Constructed once per REPL session. ``record(req, resp)`` pushes a
    completed turn. ``recent()`` exposes the rolling window to the
    REPL for context injection. ``reset()`` clears (used by
    ``/clear`` and nation switches).
    """

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._turns: deque[Turn] = deque(maxlen=maxlen)

    def record(self, request: str, response: str, timestamp: float = 0.0) -> None:
        if not request.strip():
            return
        self._turns.append(
            Turn(request=request, response=response, timestamp=timestamp)
        )

    def recent(self) -> list[Turn]:
        return list(self._turns)

    def reset(self) -> None:
        self._turns.clear()

    def last_turn(self) -> Turn | None:
        return self._turns[-1] if self._turns else None

    def __len__(self) -> int:
        return len(self._turns)

    def compressed_view(
        self,
        *,
        keep_head: int = 2,
        keep_tail: int = 4,
        summarize_fn=None,
    ) -> list["Turn"]:
        """0.1.62 — head-tail preservation with optional middle compression.

        Inspired by hermes ``agent/context_compressor.py``. When the
        rolling window holds more turns than (head + tail), the middle
        turns are replaced by a single synthetic Turn carrying either:

          - a real summary from ``summarize_fn(middle_turns) -> str``
            (caller-supplied; typically an LLM call), or
          - a lossy placeholder ``[N earlier turns omitted]`` when
            ``summarize_fn`` is None.

        Why preserve HEAD: the first few turns anchor the conversation
        ("what we're working on", style preferences, project context).
        Dropping them loses identity.

        Why preserve TAIL: the most recent turns are what the user is
        immediately continuing from. Dropping them breaks follow-ups.

        ``keep_head + keep_tail`` defaults to 6 because that's the
        smallest window where the strategy actually helps — below
        that, just return the deque verbatim.

        Returns a new list; does NOT mutate the stored deque. Caller
        decides whether to use the compressed view for one ask
        (e.g. wrap_with_context) or commit it back.
        """
        turns = list(self._turns)
        if len(turns) <= keep_head + keep_tail:
            return turns

        head = turns[:keep_head]
        tail = turns[-keep_tail:]
        middle = turns[keep_head:-keep_tail]
        if not middle:
            return turns  # nothing to compress

        if summarize_fn is not None:
            try:
                summary_text = summarize_fn(middle)
            except Exception:  # noqa: BLE001
                # If user-supplied summarizer fails, fall back to the
                # lossy marker — never break the conversation context.
                summary_text = None
        else:
            summary_text = None

        if not summary_text:
            summary_text = f"[{len(middle)} earlier turn(s) omitted]"

        # The synthetic turn carries an empty request (so follow-up
        # detection doesn't mistake it for a user message) and the
        # summary in the response slot. timestamp = midpoint of the
        # compressed range so chronology stays coherent.
        midpoint_ts = (middle[0].timestamp + middle[-1].timestamp) / 2.0
        synthetic = Turn(
            request="",
            response=summary_text,
            timestamp=midpoint_ts,
        )
        return head + [synthetic] + tail

    def compress_in_place(
        self,
        *,
        keep_head: int = 2,
        keep_tail: int = 4,
        summarize_fn=None,
    ) -> int:
        """Replace the stored deque with the compressed view.

        Returns the number of middle turns that were collapsed (0
        means no compression happened — useful for the REPL's
        ``/compress`` command output).
        """
        before = len(self._turns)
        compressed = self.compressed_view(
            keep_head=keep_head,
            keep_tail=keep_tail,
            summarize_fn=summarize_fn,
        )
        if len(compressed) >= before:
            return 0  # nothing happened
        # Replace contents preserving maxlen.
        maxlen = self._turns.maxlen
        self._turns = deque(compressed, maxlen=maxlen)
        return before - len(compressed)


def is_follow_up(current: str, prior: Turn | None) -> bool:
    """Heuristic: does ``current`` reference the prior turn?

    We use cheap signals only — no LLM round-trip. Three cues:

    1. ``current`` is short (< 12 words) AND ``prior`` exists. Most
       genuine standalone asks are longer than this once the user
       commits to a question.
    2. ``current`` contains any follow-up marker keyword. These were
       chosen by reading the actual screenshots users sent and
       picking what they said when they meant "I'm continuing."
    3. ``current`` starts with a connector ("And", "But", "What
       about", "那么" etc) — covered by the marker list.

    False positives here are OK: at worst Scout sees a context block
    it doesn't strictly need and writes a slightly heavier prompt.
    False negatives are the real cost: that's the bug we're closing.

    0.1.48 exception: trivial pleasantries ("你好", "hi", "thanks")
    are NEVER follow-ups. A greeting after a bug-analysis turn means
    "let me start fresh", not "continue analyzing the bug". The
    pre-0.1.48 short-text rule incorrectly wrapped these with bug-
    analysis context, which made Scout treat "你好" as complex and
    blew 45s on a real session. is_trivial_request lives in
    skill_match; we re-import locally to avoid a circular dep.
    """
    if prior is None:
        return False
    text = current.strip()
    if not text:
        return False
    # 0.1.48 — trivial-reset guard. Cheap import here so module load
    # doesn't depend on the skill subsystem.
    from anthill.core.skill_match import is_trivial_request
    if is_trivial_request(text):
        return False
    # 0.1.53 — URL-bearing requests are fresh tasks. The URL IS the
    # context the user is asking about; wrapping with stale prior
    # turns adds noise. Real-user case: "分析下：http://..." (2 tokens)
    # otherwise hits the word_count <= 5 path below and inherits the
    # last bug-analysis turn's framing.
    from anthill.core.url_attachments import has_url
    if has_url(text) or "@" in text:
        return False
    lower = text.lower()

    # Marker hit is sufficient regardless of length. Uses the same
    # tier-aware matcher as fast_classify so "X and Y" in the middle
    # of a long fresh question doesn't accidentally fire.
    if has_follow_up_marker(lower):
        return True

    # Short ambiguous follow-ups: "那张图怎么回事" / "tell me about it"
    # — too generic to know without checking prior. Use length as
    # the deciding cue once markers don't fire.
    word_count = len(text.split())
    if word_count <= 5:
        # Strong enough signal: short input + we have prior context.
        # Wrapping with context is the safer default; over-wrapping
        # costs tokens, under-wrapping costs the user's trust.
        return True

    return False


def _truncate_middle(text: str, max_chars: int) -> str:
    """Keep head and tail; replace middle with an ellipsis when needed."""
    if len(text) <= max_chars:
        return text
    keep = (max_chars - 5) // 2
    return f"{text[:keep]}\n…\n{text[-keep:]}"


def wrap_with_context(
    current: str,
    history: Iterable[Turn],
    *,
    max_chars_per_turn: int = DEFAULT_TURN_CHARS,
) -> str:
    """Build the actual prompt that reaches Scout.

    Layout (intentionally simple so Scout's JSON-output bias stays
    intact):

        [recent conversation — read these before planning]
        TURN 1:
          user: ...
          assistant: ...
        TURN 2:
          ...
        ---
        [current ask, treat as a follow-up to the conversation above]
        <current>

    The header phrasing is deliberate: Scout already knows to
    distinguish ``<user_request>`` content from instructions
    (see the SECURITY paragraph in SCOUT_SYSTEM_PROMPT_TEMPLATE).
    Context blocks live OUTSIDE that wrapping so they're advisory,
    not authoritative.
    """
    turns = list(history)
    if not turns:
        return current

    parts = ["[recent conversation — read these before planning]"]
    for i, t in enumerate(turns, start=1):
        req = _truncate_middle(t.request.strip(), max_chars_per_turn)
        resp = _truncate_middle(t.response.strip(), max_chars_per_turn)
        parts.append(f"TURN {i}:\n  user: {req}\n  assistant: {resp}")
    parts.append("---")
    parts.append("[current ask, treat as a follow-up to the conversation above]")
    parts.append(current)
    return "\n\n".join(parts)
