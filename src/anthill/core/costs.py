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
# 0.1.19 — refreshed against each provider's official pricing docs
# in May 2026. Legacy ids (deepseek-chat / deepseek-reasoner /
# claude-3-5-*) intentionally kept here as best-effort cost lookup
# for any old history rows — calling them now will fail, but reading
# back cost data on past runs should still work.
_DEFAULT_PRICES_USD: dict[str, tuple[float, float]] = {
    # --- DeepSeek (api-docs.deepseek.com) ---
    "deepseek-v4-pro": (0.55, 2.19),
    "deepseek-v4-flash": (0.14, 0.55),
    # Legacy — kept for backward cost lookup; retiring 2026-07-24.
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),

    # --- MiniMax (platform.minimax.io) ---
    "MiniMax-M2.7": (0.30, 1.20),
    "MiniMax-M2.7-highspeed": (0.15, 0.60),
    "MiniMax-M2.5": (0.30, 1.20),
    "MiniMax-M2.1": (0.30, 1.20),
    # Legacy MiniMax names — kept for old-history cost reads.
    "minimax": (1.00, 3.00),
    "MiniMax-M2-Stable": (0.30, 1.20),
    "MiniMax-M2": (0.30, 1.20),

    # --- OpenAI (developers.openai.com/api/docs/pricing) ---
    "gpt-5.5": (2.50, 10.00),
    "gpt-5.5-pro": (15.00, 60.00),
    "gpt-5.4": (1.25, 5.00),
    "gpt-5.4-pro": (10.00, 40.00),
    "gpt-5.4-mini": (0.25, 1.00),
    "gpt-5.4-nano": (0.10, 0.40),
    "gpt-5.3-codex": (1.25, 5.00),
    "o3": (2.00, 8.00),
    "o3-pro": (20.00, 80.00),

    # --- Anthropic (platform.claude.com/docs/.../pricing) ---
    "claude-opus-4-7": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1": (15.00, 75.00),
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
