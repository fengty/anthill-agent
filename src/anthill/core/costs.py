"""Cost tracking — every API call costs tokens, and tokens cost money.

We record token counts on every TaskResult already (input_tokens,
output_tokens). This module:

    1. Persists per-task usage to usage.jsonl
    2. Knows the per-million-token price of common models
    3. Aggregates by citizen, task_type, model, and time window

The king can run `anthill costs` and see how the budget is being spent —
who is expensive, what kind of work is expensive, whether the nation is
on track for a monthly budget.

Prices below are best-effort 2026 list prices for the models the project
supports. They are user-overridable via PRICE_PER_MILLION_INPUT /
PRICE_PER_MILLION_OUTPUT env vars when running anthill, in case the user
has a different rate (volume discounts, regional pricing).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# Per million tokens, USD. Conservative public list prices.
_DEFAULT_PRICES_USD: dict[str, tuple[float, float]] = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "minimax": (1.00, 3.00),
    "MiniMax-M2-Stable": (0.30, 1.20),
    "MiniMax-M2": (0.30, 1.20),
    "MiniMax-M2.5": (0.30, 1.20),
    "minimax-m2-stable": (0.30, 1.20),
    "minimax-m2": (0.30, 1.20),
    "minimax-m2.5": (0.30, 1.20),
}


def price_for(model: str) -> tuple[float, float]:
    """Return (input, output) USD per million tokens for a model."""
    override_in = os.getenv("ANTHILL_PRICE_INPUT_PER_M")
    override_out = os.getenv("ANTHILL_PRICE_OUTPUT_PER_M")
    if override_in and override_out:
        return float(override_in), float(override_out)
    return _DEFAULT_PRICES_USD.get(model, (1.00, 3.00))  # fallback


@dataclass
class UsageRecord:
    """One row per executed subtask attempt."""

    timestamp: float
    agent_id: str
    model: str
    task_type: str
    input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float:
        in_per_m, out_per_m = price_for(self.model)
        return (
            self.input_tokens * in_per_m / 1_000_000
            + self.output_tokens * out_per_m / 1_000_000
        )


def usage_path(nation_dir: Path) -> Path:
    return nation_dir / "usage.jsonl"


def append_usage(record: UsageRecord, nation_dir: Path) -> None:
    nation_dir.mkdir(parents=True, exist_ok=True)
    with usage_path(nation_dir).open("a") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": record.timestamp,
                    "agent_id": record.agent_id,
                    "model": record.model,
                    "task_type": record.task_type,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                }
            )
            + "\n"
        )


def load_usage(nation_dir: Path) -> list[UsageRecord]:
    path = usage_path(nation_dir)
    if not path.exists():
        return []
    records: list[UsageRecord] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            records.append(
                UsageRecord(
                    timestamp=data["timestamp"],
                    agent_id=data["agent_id"],
                    model=data["model"],
                    task_type=data["task_type"],
                    input_tokens=data["input_tokens"],
                    output_tokens=data["output_tokens"],
                )
            )
    return records


@dataclass
class CostReport:
    total_input: int
    total_output: int
    total_cost_usd: float
    by_model: dict[str, float]
    by_task_type: dict[str, float]
    by_agent: dict[str, float]
    period_start: float | None
    period_end: float | None


def summarise(records: list[UsageRecord], *, since: float | None = None) -> CostReport:
    """Aggregate usage records into a CostReport."""
    filtered = [r for r in records if (since is None or r.timestamp >= since)]
    by_model: dict[str, float] = {}
    by_task: dict[str, float] = {}
    by_agent: dict[str, float] = {}
    total_in = 0
    total_out = 0
    total_cost = 0.0
    for r in filtered:
        total_in += r.input_tokens
        total_out += r.output_tokens
        c = r.cost_usd
        total_cost += c
        by_model[r.model] = by_model.get(r.model, 0.0) + c
        by_task[r.task_type] = by_task.get(r.task_type, 0.0) + c
        by_agent[r.agent_id] = by_agent.get(r.agent_id, 0.0) + c
    return CostReport(
        total_input=total_in,
        total_output=total_out,
        total_cost_usd=total_cost,
        by_model=by_model,
        by_task_type=by_task,
        by_agent=by_agent,
        period_start=min((r.timestamp for r in filtered), default=None),
        period_end=max((r.timestamp for r in filtered), default=None),
    )
