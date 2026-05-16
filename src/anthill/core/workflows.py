"""Workflow templates — procedural memory above the per-request level.

`plan_cache` already memoises specific requests. But the nation also
discovers higher-level patterns:

    "Most translate-style requests follow translate -> verify"
    "Most research-style requests follow research -> compare -> recommend"

A WorkflowTemplate captures one such recurring plan shape. When a new
request arrives whose decomposed plan would *look like* an existing
template, Scout can be asked to follow that shape — gaining the speed
benefit of caching with the flexibility of fresh decomposition.

Templates are mined from history (success only, repeat count >=
min_recurrence) and stored in workflows.json. They're the
'workflow' analog of facts.md — durable, audit-able, optional.

This module ships the miner and the storage; the runtime use (Scout
shape hints) is a follow-up that can be added without breaking changes.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from anthill.core.history import HistoryEntry


@dataclass
class WorkflowTemplate:
    """One recurring plan shape."""

    shape: tuple[str, ...]  # ordered task_types
    occurrences: int
    success_rate: float  # success ratio across observed occurrences

    @property
    def signature(self) -> str:
        return " -> ".join(self.shape)

    def to_dict(self) -> dict:
        return {
            "shape": list(self.shape),
            "occurrences": self.occurrences,
            "success_rate": self.success_rate,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowTemplate":
        return cls(
            shape=tuple(data["shape"]),
            occurrences=int(data["occurrences"]),
            success_rate=float(data["success_rate"]),
        )


def workflows_path(nation_dir: Path) -> Path:
    return nation_dir / "workflows.json"


def save_workflows(templates: list[WorkflowTemplate], nation_dir: Path) -> Path:
    nation_dir.mkdir(parents=True, exist_ok=True)
    path = workflows_path(nation_dir)
    path.write_text(json.dumps([t.to_dict() for t in templates], indent=2))
    return path


def load_workflows(nation_dir: Path) -> list[WorkflowTemplate]:
    path = workflows_path(nation_dir)
    if not path.exists():
        return []
    return [WorkflowTemplate.from_dict(d) for d in json.loads(path.read_text())]


def mine_workflows(
    history: list[HistoryEntry],
    *,
    min_recurrence: int = 2,
    min_steps: int = 2,
) -> list[WorkflowTemplate]:
    """Find recurring plan shapes in history.

    A 'shape' is the ordered tuple of task_types in a plan.
    Templates are returned sorted by occurrences desc.
    """
    seen_counts: Counter[tuple[str, ...]] = Counter()
    success_counts: Counter[tuple[str, ...]] = Counter()

    for entry in history:
        shape = tuple(s["task_type"] for s in entry.plan)
        if len(shape) < min_steps:
            continue
        seen_counts[shape] += 1
        if all(o["status"] == "ok" for o in entry.outcomes):
            success_counts[shape] += 1

    templates: list[WorkflowTemplate] = []
    for shape, count in seen_counts.items():
        if count < min_recurrence:
            continue
        rate = (success_counts[shape] / count) if count else 0.0
        templates.append(
            WorkflowTemplate(shape=shape, occurrences=count, success_rate=rate)
        )
    templates.sort(key=lambda t: (t.occurrences, t.success_rate), reverse=True)
    return templates


def format_templates_for_scout(templates: list[WorkflowTemplate], top_k: int = 5) -> str:
    """Concise context block listing the nation's known workflow shapes."""
    if not templates:
        return ""
    lines = ["Known workflow shapes in this nation (consider matching one if it fits):"]
    for t in templates[:top_k]:
        lines.append(
            f"  - {t.signature}  ({t.occurrences} runs, {t.success_rate:.0%} success)"
        )
    return "\n".join(lines)
