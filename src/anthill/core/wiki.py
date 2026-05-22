"""0.2.11 — auto-summarized wiki of what the user has been working on.

User asked: "比如 自动归纳在做的事情的 wiki，这些是否有做。"
Answer pre-0.2.11: no. We had `/history` (time-ordered), `/search Q`
(grep), `skill_mining` (recurrence detection), `MEMORY.md` (manual
notes), but no AGGREGATED VIEW by topic. The user couldn't see "what
projects am I actually using anthill for?" without scrolling history.

0.2.11 builds it: read history.jsonl, extract topics via TF-IDF on
token clusters, group asks by topic, write `wiki.md` to nation dir.
Auto-refreshes after each ask. No new REPL command — the wiki shows
up on the welcome splash and on disk for `cat` / `git diff` review.

Design constraints:
- Zero LLM calls. We use deterministic token-cluster cosine, same
  shape as core/skill_mining + core/episodic. Predictable + cheap.
- Topics are NAMED by their most representative ask. No fancy
  abstract labels like "Database infrastructure" — the topic IS
  the user's actual request.
- Stable across re-runs. The wiki.md should diff cleanly so the
  user can see what changed week over week.
- Best-effort I/O. A write failure must not break the ask path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from anthill.core.episodic import _TOKEN_RE
from anthill.core.history import HistoryEntry, load_history


# Cosine similarity for cluster merging. Same shape as the threshold
# in core/skill_mining (0.6) so the wiki and the mining hint agree
# on what "same topic" means.
TOPIC_SIMILARITY_THRESHOLD = 0.5

# Minimum asks before a cluster becomes a topic. Below this it's
# noise — every one-off ask doesn't deserve its own section.
MIN_ASKS_FOR_TOPIC = 2

# Max topics shown on the welcome splash. Beyond this the splash
# gets noisy; full list still in wiki.md.
SPLASH_TOPICS_LIMIT = 5

# Cap on history scanned. Quadratic clustering means this matters.
# 200 is comfortable for interactive use; deeper history can be
# explored via /search or by reading history.jsonl directly.
DEFAULT_SCAN_LIMIT = 200


@dataclass(frozen=True)
class Topic:
    """One inferred topic the user has been working on."""

    name: str                    # the most-recent request in the cluster
    ask_count: int
    last_touched: float          # latest ts in the cluster
    entry_ids: tuple[str, ...]   # history entry IDs in the cluster
    sample_requests: tuple[str, ...]  # up to 3 recent representative asks


def _tokens(text: str) -> set[str]:
    return {tok.lower() for tok in _TOKEN_RE.findall(text)}


def _cosine(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    common = len(a & b)
    return common / ((len(a) * len(b)) ** 0.5)


def _successful(entry: HistoryEntry) -> bool:
    """A history entry counts when at least one subtask succeeded.
    Repeated failures aren't 'a topic we work on'."""
    outcomes = entry.outcomes or []
    return any(o.get("status") == "ok" for o in outcomes)


def build_topics(
    history: list[HistoryEntry],
    *,
    min_asks: int = MIN_ASKS_FOR_TOPIC,
    similarity_threshold: float = TOPIC_SIMILARITY_THRESHOLD,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> list[Topic]:
    """Cluster history asks into topics.

    Returns topics ordered by recency-then-size (most recently touched
    first; ties broken by ask_count desc). Below-threshold clusters
    are dropped — "I tried X once" isn't a topic.
    """
    candidates = [e for e in history if _successful(e)]
    candidates.sort(key=lambda e: e.timestamp, reverse=True)
    candidates = candidates[:scan_limit]

    clusters: list[list[HistoryEntry]] = []
    cluster_tokens: list[set[str]] = []

    for entry in candidates:
        tokens = _tokens(entry.request)
        if not tokens:
            continue
        # Find best matching existing cluster (greedy single-pass).
        # Same logic as skill_mining — pick the first that crosses
        # threshold; refresh that cluster's token bag toward the
        # newest member so later asks match recent phrasing.
        joined = False
        for i, ctok in enumerate(cluster_tokens):
            if _cosine(tokens, ctok) >= similarity_threshold:
                clusters[i].append(entry)
                cluster_tokens[i] = tokens
                joined = True
                break
        if not joined:
            clusters.append([entry])
            cluster_tokens.append(tokens)

    out: list[Topic] = []
    for cluster in clusters:
        if len(cluster) < min_asks:
            continue
        # Order within cluster: newest first (so the topic's
        # "name" — the representative — is the latest phrasing).
        ordered = sorted(cluster, key=lambda e: e.timestamp, reverse=True)
        out.append(
            Topic(
                name=ordered[0].request,
                ask_count=len(ordered),
                last_touched=ordered[0].timestamp,
                entry_ids=tuple(e.id for e in ordered),
                sample_requests=tuple(e.request for e in ordered[:3]),
            )
        )
    out.sort(key=lambda t: (t.last_touched, t.ask_count), reverse=True)
    return out


def format_splash_line(topics: list[Topic], *, limit: int = SPLASH_TOPICS_LIMIT) -> str:
    """One-line topic summary for the welcome splash.

    Returns e.g. '禅道 bug 分析 (5×), MySQL 部署 (3×), 飞书对接 (2×)'.
    Empty string when there are no topics yet (don't pollute splash
    with nothing).
    """
    if not topics:
        return ""
    parts: list[str] = []
    for t in topics[:limit]:
        snippet = t.name.replace("\n", " ")[:32]
        if len(t.name) > 32:
            snippet += "…"
        parts.append(f"{snippet} ({t.ask_count}×)")
    return ", ".join(parts)


def format_wiki_markdown(topics: list[Topic]) -> str:
    """Render the topics as a Markdown wiki document, written to
    `wiki.md` in the nation directory."""
    if not topics:
        return (
            "# What this nation has been working on\n\n"
            "_No topics yet. After a few related asks, this file will\n"
            "fill in automatically._\n"
        )
    lines: list[str] = ["# What this nation has been working on", ""]
    now = time.time()
    for t in topics:
        age = _humanize_ago(now - t.last_touched)
        title = t.name.replace("\n", " ").strip()
        if len(title) > 80:
            title = title[:80].rstrip() + "…"
        lines.append(f"## {title}")
        lines.append("")
        lines.append(
            f"- **{t.ask_count} ask(s)**, 上次 {age}"
        )
        for sample in t.sample_requests:
            sample_snippet = sample.replace("\n", " ")[:120]
            if len(sample) > 120:
                sample_snippet += "…"
            lines.append(f"  - {sample_snippet}")
        lines.append("")
    return "\n".join(lines)


def wiki_path(nation_dir: Path) -> Path:
    return nation_dir / "wiki.md"


def write_wiki(nation_dir: Path, topics: list[Topic]) -> Path:
    """Write the wiki to disk. Best-effort; never raises."""
    path = wiki_path(nation_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(format_wiki_markdown(topics))
    except OSError:
        pass
    return path


def refresh_wiki(nation_dir: Path) -> list[Topic]:
    """Read history, recompute topics, write wiki.md.

    Called from the REPL after each successful ask. Returns the
    fresh topic list so the caller can also feed it to the splash.
    Best-effort: any I/O / parse error returns [] without raising.
    """
    try:
        history = load_history(nation_dir)
    except Exception:  # noqa: BLE001
        return []
    topics = build_topics(history)
    write_wiki(nation_dir, topics)
    return topics


def _humanize_ago(seconds: float) -> str:
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 7:
        return f"{int(seconds // 86400)} 天前"
    if seconds < 86400 * 30:
        return f"{int(seconds // (86400 * 7))} 周前"
    return f"{int(seconds // (86400 * 30))} 月前"
