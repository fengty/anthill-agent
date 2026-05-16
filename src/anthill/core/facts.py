"""Semantic memory — distill durable facts from accumulated experience.

History gives us episodic memory (specific events). Pheromones give us
procedural memory (which citizen does what). What's missing is the
middle layer — **facts** that summarise what the nation has *learned*
about the world it operates in.

Examples:
    - "The king prefers terse answers when the request mentions code."
    - "Translate tasks go to deepseek citizens; explain tasks vary."
    - "Multi-step research plans usually succeed; single-step research often fails."
    - "Most requests come on weekdays."

These are not skills. They are not history. They are propositions the
nation has empirical reason to believe.

This module collects them in two ways:

1. **Derived facts** (deterministic): computed mechanically from
   history + pheromones. No LLM call. Cheap, accurate.

2. **Inferred facts** (LLM-assisted): the nation periodically asks the
   Scout-class model to read a sample of history and propose
   higher-level patterns. Slower, broader, but needs review.

For now we ship (1) and expose (2) as an optional command. The
distillation pipeline writes a single facts.md the nation can attach
to system prompts as condensed semantic memory.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from anthill.core.history import HistoryEntry
from anthill.core.pheromone import PheromoneTrail


@dataclass
class Fact:
    """One durable observation about the nation."""

    statement: str
    evidence_count: int  # how many history entries / trails support this
    category: str        # "preference" / "specialist" / "pattern" / "workflow"

    def as_line(self) -> str:
        return f"- {self.statement}  [evidence: {self.evidence_count}]"


def facts_path(nation_dir: Path) -> Path:
    return nation_dir / "facts.md"


def write_facts(facts: list[Fact], nation_dir: Path) -> Path:
    nation_dir.mkdir(parents=True, exist_ok=True)
    path = facts_path(nation_dir)
    grouped: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        grouped[f.category].append(f)

    lines = ["# Facts distilled from this nation's experience", ""]
    for category in sorted(grouped):
        lines.append(f"## {category.title()}")
        lines.append("")
        for f in grouped[category]:
            lines.append(f.as_line())
        lines.append("")
    path.write_text("\n".join(lines))
    return path


def read_facts(nation_dir: Path) -> str:
    path = facts_path(nation_dir)
    return path.read_text() if path.exists() else ""


def derive_facts(
    history: list[HistoryEntry],
    pheromones: PheromoneTrail,
    *,
    min_evidence: int = 2,
) -> list[Fact]:
    """Compute deterministic facts from current state.

    `min_evidence` filters out one-shot observations that would otherwise
    be presented as established beliefs.
    """
    facts: list[Fact] = []
    facts.extend(_specialist_facts(pheromones, min_evidence=min_evidence))
    facts.extend(_workflow_facts(history, min_evidence=min_evidence))
    facts.extend(_pattern_facts(history, min_evidence=min_evidence))
    return facts


def _specialist_facts(pheromones: PheromoneTrail, min_evidence: int) -> list[Fact]:
    """Strong trail = nation knows who's good at this task type."""
    by_type: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for trail in pheromones.trails():
        if trail.strength - trail.alarm >= float(min_evidence):
            by_type[trail.task_type].append((trail.agent_id, trail.strength - trail.alarm))

    facts: list[Fact] = []
    for task_type, candidates in by_type.items():
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_id, score = candidates[0]
        facts.append(
            Fact(
                statement=f"{best_id} is the strongest specialist for '{task_type}' (net pheromone {score:.1f}).",
                evidence_count=int(score),
                category="specialist",
            )
        )
    return facts


def _workflow_facts(history: list[HistoryEntry], min_evidence: int) -> list[Fact]:
    """Recurring plan shapes that have succeeded multiple times."""
    shape_counts: Counter[tuple[str, ...]] = Counter()
    for entry in history:
        ok = all(o["status"] == "ok" for o in entry.outcomes)
        if not ok:
            continue
        shape = tuple(s["task_type"] for s in entry.plan)
        if len(shape) >= 2:
            shape_counts[shape] += 1

    facts: list[Fact] = []
    for shape, count in shape_counts.items():
        if count < min_evidence:
            continue
        chain = " -> ".join(shape)
        facts.append(
            Fact(
                statement=f"Plan shape '{chain}' has succeeded {count} times.",
                evidence_count=count,
                category="workflow",
            )
        )
    return facts


def _pattern_facts(history: list[HistoryEntry], min_evidence: int) -> list[Fact]:
    """Aggregate patterns over history: task-type frequency, success ratio."""
    if len(history) < min_evidence:
        return []

    total = 0
    ok = 0
    by_type: Counter[str] = Counter()
    for entry in history:
        for outcome in entry.outcomes:
            total += 1
            if outcome["status"] == "ok":
                ok += 1
            by_type[outcome["task_type"]] += 1
    if total == 0:
        return []

    facts: list[Fact] = []
    success_rate = ok / total
    facts.append(
        Fact(
            statement=f"Overall subtask success rate is {success_rate:.0%} ({ok}/{total}).",
            evidence_count=total,
            category="pattern",
        )
    )
    if by_type:
        most_common, count = by_type.most_common(1)[0]
        facts.append(
            Fact(
                statement=f"Most common task type is '{most_common}' ({count} occurrences).",
                evidence_count=count,
                category="pattern",
            )
        )
    return facts
