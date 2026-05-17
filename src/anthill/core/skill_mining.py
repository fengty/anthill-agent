"""0.1.17 — skill auto-mining: detect repeated ask patterns.

Different from ``core/workflows.py``, which mines *plan shapes* —
the sequence of task_types — across history. This module mines
*request shapes*: when a user has asked something semantically
similar three or more times, suggest crystallizing it into a named
recipe so the user owns the abstraction explicitly.

The detection is intentionally simple — bag-of-tokens cosine, same
shape as ``core/episodic.py``. Heavy NLP would be the wrong knob to
turn here: we just want to notice "you keep saying 'translate ... to
French and explain'" and surface it once. The user picks the name.

Outputs ``MinedSkill`` instances ordered by recurrence-then-recency.
Successful asks only — repeating a failing query is not a skill.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from anthill.core.episodic import _TOKEN_RE
from anthill.core.history import HistoryEntry


# Cosine similarity threshold for two requests to count as "the same".
# 0.6 catches "translate X to French" and "translate Y to French and
# explain choices" without sticking together obviously unrelated asks.
DEFAULT_SIMILARITY_THRESHOLD = 0.6

# Minimum repeats before we'll suggest the user save as a recipe.
DEFAULT_MIN_OCCURRENCES = 3

# How many recent successful entries to scan. Bigger = more catches,
# but quadratic — 100 is a comfortable upper bound for interactive use.
DEFAULT_SCAN_LIMIT = 100


@dataclass(frozen=True)
class MinedSkill:
    """A request pattern that recurs in history.

    ``representative`` is the canonical request (the most recent entry
    in the cluster — most likely to match what the user types next).
    ``occurrences`` is the cluster size. ``entry_ids`` are the
    history IDs in the cluster so the REPL can show provenance.
    """

    representative: str
    occurrences: int
    entry_ids: tuple[str, ...]
    latest_timestamp: float


def _tokenize(text: str) -> set[str]:
    """Lowercase tokenization matching core/episodic._tokenize."""
    return {tok.lower() for tok in _TOKEN_RE.findall(text)}


def _cosine(a: set[str], b: set[str]) -> float:
    """Set-cosine similarity. 1.0 when identical token bags, 0.0 disjoint."""
    if not a or not b:
        return 0.0
    common = len(a & b)
    return common / ((len(a) * len(b)) ** 0.5)


def _successful(entry: HistoryEntry) -> bool:
    """A history entry counts as successful when at least one subtask succeeded.

    Same rule the REPL splash uses for the welcome-back counter, kept
    in sync deliberately — both views of "what the user has actually
    done" should agree.
    """
    outcomes = entry.outcomes or []
    return any(o.get("status") == "ok" for o in outcomes)


def mine_skills(
    history: list[HistoryEntry],
    *,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> list[MinedSkill]:
    """Find request-text clusters that recur at least ``min_occurrences`` times.

    Returns clusters ordered by occurrences (desc), then by latest
    timestamp (desc). Empty history / no clusters reaching threshold
    return an empty list — callers should treat that as "nothing
    interesting yet" and stay quiet.
    """
    # Only successful, recent-first, capped at scan_limit.
    candidates = [e for e in history if _successful(e)]
    candidates.sort(key=lambda e: e.timestamp, reverse=True)
    candidates = candidates[:scan_limit]

    # Single-pass clustering: each entry joins the first existing
    # cluster it's similar to; otherwise it seeds a new one. Not the
    # most accurate clustering known to humans, but it's deterministic
    # and good enough for "you've done X three times" UX.
    clusters: list[list[HistoryEntry]] = []
    cluster_tokens: list[set[str]] = []
    for entry in candidates:
        tokens = _tokenize(entry.request)
        if not tokens:
            continue
        joined = False
        for i, cluster_tok in enumerate(cluster_tokens):
            if _cosine(tokens, cluster_tok) >= similarity_threshold:
                clusters[i].append(entry)
                # Refresh the cluster's token bag toward the most
                # recent member so later entries match the freshest
                # phrasing rather than the very first one.
                cluster_tokens[i] = tokens
                joined = True
                break
        if not joined:
            clusters.append([entry])
            cluster_tokens.append(tokens)

    out: list[MinedSkill] = []
    for cluster in clusters:
        if len(cluster) < min_occurrences:
            continue
        # Most recent entry is the representative — likeliest to
        # match what the user is about to type again.
        cluster_sorted = sorted(cluster, key=lambda e: e.timestamp, reverse=True)
        rep = cluster_sorted[0]
        out.append(
            MinedSkill(
                representative=rep.request,
                occurrences=len(cluster),
                entry_ids=tuple(e.id for e in cluster_sorted),
                latest_timestamp=rep.timestamp,
            )
        )
    out.sort(key=lambda s: (-s.occurrences, -s.latest_timestamp))
    return out


def looks_like_new_match(skill: MinedSkill, request: str) -> bool:
    """Cheap check: is ``request`` similar enough to ``skill`` to count?

    Used by the post-ask hint to decide whether to surface the
    suggestion *this* turn. Reuses the same threshold so detection
    and surfacing don't disagree.
    """
    return _cosine(_tokenize(skill.representative), _tokenize(request)) >= DEFAULT_SIMILARITY_THRESHOLD


def freshness_window_days(skill: MinedSkill, *, now: float | None = None) -> float:
    """Days since the most recent ask in the cluster — for surfacing freshness."""
    now = now if now is not None else time.time()
    return (now - skill.latest_timestamp) / 86400.0
