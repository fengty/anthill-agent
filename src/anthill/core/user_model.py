"""0.1.32 — implicit user-model inference from behavior signals.

0.1.30 catches *explicit* "remember X" markers in user input. This
patch closes the implicit half: detect preferences from HOW the user
actually uses the nation — what language they type in, what topics
they keep asking about, what kinds of answers they rate up vs down.
Each inference becomes a candidate line for USER.md, surfaced to the
user (not silently written) so they can confirm or correct.

The "越来越像你" payoff: without this, USER.md only grows when the
user remembers to say "remember I prefer X." With this, the nation
periodically says "I've noticed Y about you — should I remember it?"

Signal types implemented in 0.1.32:

  - **Language bias** — Chinese-first vs English-first, derived from
    char-class ratios over recent requests. Single-shot inference;
    re-runs each time the window of recent asks rolls forward.

  - **Length preference** — comparing rated-up outputs vs rated-down
    outputs by length, when enough exemplars exist. Tells us if the
    user wants concise / detailed answers.

  - **Topic focus** — top 3 task_types by recent volume + skill-mining
    cluster sizes. "Mostly asks about: research, translate, code."

  - **Time-of-day pattern** — when most asks land (working hours /
    evening / late-night), surfaced as a working-style hint.

Future signals (left for later patches):
  - Style: terseness, formality, code-vs-prose preference
  - Bilingual code-switch frequency
  - "Always asks for sources" / "wants TL;DR first"

Each inference carries a confidence in [0, 1]. The REPL surfaces
inferences above 0.7 and lets the user accept them with `/profile
accept` (added in this patch).
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from dataclasses import dataclass, field

from anthill.core.feedback import Exemplar
from anthill.core.history import HistoryEntry


# Window over which we look at recent activity. Big enough for stable
# signals, small enough that the inference adapts to a changing user.
DEFAULT_WINDOW = 30

# Minimum confidence to surface to the user.
MIN_CONFIDENCE = 0.7


# Inference kinds — keep stable; written into USER.md as the dedup key.
KIND_LANGUAGE = "language"
KIND_LENGTH = "length"
KIND_TOPICS = "topics"
KIND_TIME = "time-of-day"


@dataclass(frozen=True)
class Inference:
    """One implicit-signal observation about the user."""

    kind: str             # KIND_* constant
    summary: str          # short human-readable line, ready to insert
    confidence: float     # [0, 1]
    suggested_section: str = "Preferences"   # which USER.md section
    evidence: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def infer_user_model(
    history: list[HistoryEntry],
    exemplars: list[Exemplar] | None = None,
    *,
    window: int = DEFAULT_WINDOW,
    now: float | None = None,
) -> list[Inference]:
    """Run every signal-extractor; return ones above MIN_CONFIDENCE.

    ``history`` and ``exemplars`` are read-only inputs — the function
    is pure so it can be unit-tested without filesystem state.
    """
    recent = history[-window:] if window > 0 else history
    out: list[Inference] = []
    if recent:
        lang = _infer_language(recent)
        if lang is not None:
            out.append(lang)
        topics = _infer_topics(recent)
        if topics is not None:
            out.append(topics)
        time_pref = _infer_time_of_day(recent, now=now)
        if time_pref is not None:
            out.append(time_pref)
    if exemplars:
        length = _infer_length_preference(exemplars)
        if length is not None:
            out.append(length)
    return [inf for inf in out if inf.confidence >= MIN_CONFIDENCE]


# ---------------------------------------------------------------------------
# Per-signal extractors
# ---------------------------------------------------------------------------


def _infer_language(entries: list[HistoryEntry]) -> Inference | None:
    """Chinese-first vs English-first from character-class ratios.

    Counts CJK Unified Ideographs vs ASCII alpha across the windowed
    requests. Ignores numbers / punctuation. Confidence scales with
    sample size + ratio extremity.
    """
    cjk = 0
    latin = 0
    for entry in entries:
        for ch in entry.request:
            cp = ord(ch)
            if 0x4E00 <= cp <= 0x9FFF:
                cjk += 1
            elif ch.isascii() and ch.isalpha():
                latin += 1
    total = cjk + latin
    if total < 20:  # too thin to call
        return None
    cjk_ratio = cjk / total
    if cjk_ratio > 0.6:
        confidence = min(1.0, 0.5 + cjk_ratio * 0.5)
        return Inference(
            kind=KIND_LANGUAGE,
            summary="prefers Chinese-first answers (auto-detected)",
            confidence=confidence,
            suggested_section="Languages / locales",
        )
    if cjk_ratio < 0.1 and latin >= 50:
        confidence = min(1.0, 0.5 + (1 - cjk_ratio) * 0.5)
        return Inference(
            kind=KIND_LANGUAGE,
            summary="prefers English-first answers (auto-detected)",
            confidence=confidence,
            suggested_section="Languages / locales",
        )
    return None


def _infer_length_preference(exemplars: list[Exemplar]) -> Inference | None:
    """Concise vs detailed, from rated outputs.

    Needs at least 3 'up' AND 3 'down' to call. Looks at the mean
    output character length in each group; large gaps signal a real
    preference. Tiny gaps mean the user rates on quality not length.
    """
    up_lens = [
        len(e.output) for e in exemplars if e.rating == "up" and e.output
    ]
    down_lens = [
        len(e.output) for e in exemplars if e.rating == "down" and e.output
    ]
    if len(up_lens) < 3 or len(down_lens) < 3:
        return None
    up_mean = statistics.mean(up_lens)
    down_mean = statistics.mean(down_lens)
    if down_mean == 0:
        return None
    ratio = up_mean / down_mean
    if ratio < 0.6:
        # Up-rated outputs are noticeably shorter than down-rated.
        return Inference(
            kind=KIND_LENGTH,
            summary="prefers concise answers (rates short replies up)",
            confidence=min(1.0, 0.6 + (1 - ratio) * 0.5),
            suggested_section="Preferences",
            evidence=(
                f"up-rated mean: {up_mean:.0f} chars",
                f"down-rated mean: {down_mean:.0f} chars",
            ),
        )
    if ratio > 1.6:
        return Inference(
            kind=KIND_LENGTH,
            summary="prefers detailed, thorough answers",
            confidence=min(1.0, 0.6 + (ratio - 1) * 0.3),
            suggested_section="Preferences",
        )
    return None


def _infer_topics(entries: list[HistoryEntry]) -> Inference | None:
    """Top task_types by frequency. Signals what the nation is FOR.

    Useful when MEMORY.md still says "(empty — describe in one line
    what you mostly ask this nation to do)" and the answer is in
    plain sight in the recent task_type distribution.
    """
    counter: Counter[str] = Counter()
    for entry in entries:
        for sub in entry.plan or []:
            tt = sub.get("task_type") if isinstance(sub, dict) else None
            if tt:
                counter[tt] += 1
    total = sum(counter.values())
    # Need a meaningful sample before declaring a topic focus. A
    # single ask doesn't tell us what this nation is for.
    if total < 8:
        return None
    top = counter.most_common(3)
    # Filter out task_types that contribute <10% — noisy one-off labels
    # shouldn't show up in the "what this nation is FOR" summary.
    headline = [tt for tt, n in top if n / total >= 0.10]
    if not headline:
        return None
    # Confidence: more samples + more concentration → higher.
    concentration = top[0][1] / total
    confidence = min(1.0, 0.5 + 0.4 * concentration + 0.1 * min(total / 50, 1.0))
    if confidence < MIN_CONFIDENCE:
        return None
    return Inference(
        kind=KIND_TOPICS,
        summary=f"mostly asks about: {', '.join(headline)}",
        confidence=confidence,
        suggested_section="Working style",
    )


def _infer_time_of_day(
    entries: list[HistoryEntry],
    *,
    now: float | None = None,
) -> Inference | None:
    """Working-hours / evening / late-night signal.

    Buckets recent ask timestamps; the dominant bucket becomes the
    inference if it covers ≥60% of asks AND there are ≥10 samples.
    """
    if len(entries) < 10:
        return None
    buckets = Counter()
    for entry in entries:
        if entry.timestamp <= 0:
            continue
        hour = time.localtime(entry.timestamp).tm_hour
        if 9 <= hour < 18:
            buckets["working hours (9-18)"] += 1
        elif 18 <= hour < 23:
            buckets["evening (18-23)"] += 1
        elif hour >= 23 or hour < 5:
            buckets["late night (23-5)"] += 1
        else:
            buckets["early morning (5-9)"] += 1
    if not buckets:
        return None
    label, count = buckets.most_common(1)[0]
    total = sum(buckets.values())
    if total == 0 or count / total < 0.60:
        return None
    confidence = min(1.0, 0.5 + 0.5 * (count / total))
    if confidence < MIN_CONFIDENCE:
        return None
    return Inference(
        kind=KIND_TIME,
        summary=f"usually works during {label}",
        confidence=confidence,
        suggested_section="Working style",
    )


# ---------------------------------------------------------------------------
# Dedup helper — used by the REPL to skip inferences already in USER.md
# ---------------------------------------------------------------------------


def already_recorded(inference: Inference, user_md_text: str) -> bool:
    """Return True if the same KIND signal is already noted in USER.md.

    We dedup by kind, not exact text, so a re-run after the user has
    edited the inferred line (or moved it to a different section)
    doesn't re-insert the same idea.
    """
    if not user_md_text:
        return False
    marker = f"<!-- auto:{inference.kind} -->"
    return marker in user_md_text
