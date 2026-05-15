"""Culture — the colony-wide layer that turns experience into identity.

Two things live here, and they answer two different questions.

The **task catalog** answers: *what does this colony do?*
Every task type the colony has ever seen, with a count. Without this,
Scout invents fresh labels for every request and the pheromone map
fragments into useless dust. With it, Scout reuses the labels the colony
already has expertise in.

The **house style** answers: *how does this colony do things?*
A free-form markdown blob that captures preferences — terse vs verbose,
formal vs casual, code-with-examples vs explain-then-code. It gets
injected as a soft constraint into every worker's system prompt.

Neither is automatic yet. Catalog grows by use; style is user-edited.
A future layer can infer style from accepted outputs, but that needs
a feedback signal we do not yet have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Culture:
    """The colony's accumulated identity, persisted to disk."""

    task_catalog: dict[str, int] = field(default_factory=dict)
    house_style: str = ""

    def record(self, task_type: str) -> None:
        """Bump the count for this task type."""
        self.task_catalog[task_type] = self.task_catalog.get(task_type, 0) + 1

    def known_task_types(self, min_count: int = 1) -> list[str]:
        """Task types with at least min_count observations, hot first."""
        items = [(tt, n) for tt, n in self.task_catalog.items() if n >= min_count]
        items.sort(key=lambda x: x[1], reverse=True)
        return [tt for tt, _ in items]

    def summarize(self) -> str:
        """A short paragraph describing the colony's identity. Used in CLI."""
        if not self.task_catalog:
            return "A young colony. No accumulated specialties yet."
        top = self.known_task_types()[:5]
        total = sum(self.task_catalog.values())
        return (
            f"Handled {total} tasks across {len(self.task_catalog)} distinct types.\n"
            f"Strongest categories: {', '.join(top)}."
        )


def culture_dir(colony_dir: Path) -> Path:
    return colony_dir / "culture"


def save_culture(culture: Culture, colony_dir: Path) -> None:
    base = culture_dir(colony_dir)
    base.mkdir(parents=True, exist_ok=True)
    (base / "catalog.json").write_text(json.dumps(culture.task_catalog, indent=2))
    (base / "house_style.md").write_text(culture.house_style)


def load_culture(colony_dir: Path) -> Culture:
    base = culture_dir(colony_dir)
    catalog: dict[str, int] = {}
    catalog_file = base / "catalog.json"
    if catalog_file.exists():
        catalog = json.loads(catalog_file.read_text())

    style = ""
    style_file = base / "house_style.md"
    if style_file.exists():
        style = style_file.read_text()

    return Culture(task_catalog=catalog, house_style=style)
