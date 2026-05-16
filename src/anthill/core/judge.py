"""LLM Judge — replace binary length-based scoring with quality assessment.

For the first 21 versions, success_score was binary: non-empty response
= 1.0, empty/exception = 0.0. That's a wire-level check, not a quality
check. A model can return well-formed garbage and score full marks.

The Judge runs after the worker, looking at the request and the output,
and produces a [0, 1] quality score. The pheromone now reinforces
quality, not just liveness.

Two design notes:

1. The judge is OPTIONAL. Many workflows do not want to pay 2x model
   cost per task. Enabled via `enable_judge=True` on the Nation or
   ANTHILL_USE_JUDGE=1 env var. Off by default.

2. The judge is asked for a SCORE plus a one-line reason, in strict
   JSON. Parsing failures fall back to neutral 0.5 rather than crash —
   a judge that doesn't return JSON is its own bug, not the worker's.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from anthill.models import get_provider


JUDGE_SYSTEM_PROMPT = """You are a strict, fair judge of agent output quality.

You will be shown a TASK and an OUTPUT. Score how well the output
satisfies the task on a 0-1 scale.

Rubric:
  1.00  Perfect: directly satisfies the task with no extra noise.
  0.75  Good:    satisfies the task, minor flaws or verbosity.
  0.50  Mixed:   partially satisfies, missing or wrong pieces.
  0.25  Weak:    barely related to the task.
  0.00  Useless: empty, off-topic, error message, or refusal.

Return ONLY a JSON object with this exact shape:

{"score": <float in [0,1]>, "reason": "<one short sentence>"}

No prose outside the JSON. No code fences.
"""


@dataclass
class Verdict:
    score: float
    reason: str


def judge_enabled() -> bool:
    """Whether the judge should run by default. Off unless env var is set."""
    return os.getenv("ANTHILL_USE_JUDGE", "").lower() in ("1", "true", "yes", "on")


def parse_verdict(text: str) -> Verdict:
    """Robust JSON extraction. Falls back to 0.5 if anything goes wrong."""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, flags=re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find a JSON object embedded in prose.
        match = re.search(r"\{[^{}]*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return Verdict(score=0.5, reason="judge returned unparseable text")
        else:
            return Verdict(score=0.5, reason="judge returned no JSON")

    raw_score = data.get("score", 0.5)
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return Verdict(score=0.5, reason="judge score not numeric")
    score = max(0.0, min(1.0, score))
    reason = str(data.get("reason", "")).strip() or "no reason given"
    return Verdict(score=score, reason=reason)


async def judge_output(
    task: str,
    output: str,
    *,
    model: str = "deepseek-chat",
) -> Verdict:
    """Ask the judge model to rate this output."""
    if not output.strip():
        return Verdict(score=0.0, reason="empty output")

    provider = get_provider(model)
    prompt = f"TASK:\n{task}\n\nOUTPUT:\n{output}"
    try:
        response = await provider.complete(
            prompt,
            system=JUDGE_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=120,
        )
    except Exception as e:  # noqa: BLE001
        return Verdict(score=0.5, reason=f"judge unavailable: {e}")
    return parse_verdict(response.text)
