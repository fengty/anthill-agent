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
from dataclasses import dataclass, field

from anthill.core.values import normalize_dim
from anthill.models import get_provider


JUDGE_SYSTEM_PROMPT = """You are a strict, fair judge of agent output quality.

You will be shown a TASK and an OUTPUT. Your job is to evaluate the
output along whichever quality dimensions are most relevant — you
pick the dimensions, you score each on a 0.0 to 1.0 scale, and you
give a one-line explanation of what each dimension means in the
context of THIS task.

Examples of dimensions you might use (you are not limited to these):
  correctness     — does it actually solve the task?
  conciseness     — is it free of filler and repetition?
  depth           — does it engage with the problem at the right level?
  tone            — does its register match the request?
  factual_grounding — are claims supported by visible evidence?
  citation        — when sources matter, are they given?
  formatting      — is the output structured the way the user asked?
  refusal_quality — when refusing, is the refusal helpful and specific?

Use whichever 2-5 dimensions matter most for this task. Invent a new
dimension name if the standard ones don't fit — short, lowercase,
snake_case, no spaces.

Also produce a single OVERALL score (0.0 to 1.0) that summarizes the
output for routing decisions.

Return ONLY a JSON object with this exact shape, no prose, no code fences:

{
  "overall": <float in [0,1]>,
  "scores": {"<dim_name>": <float in [0,1]>, ...},
  "explanations": {"<dim_name>": "<short sentence>", ...},
  "reason": "<one-line summary>"
}
"""


@dataclass
class Verdict:
    score: float  # the overall scalar; preserved for back-compat
    reason: str
    scores: dict[str, float] = field(default_factory=dict)
    explanations: dict[str, str] = field(default_factory=dict)


def judge_enabled() -> bool:
    """Whether the judge should run by default. Off unless env var is set."""
    return os.getenv("ANTHILL_USE_JUDGE", "").lower() in ("1", "true", "yes", "on")


def _coerce_score(raw: object) -> float | None:
    """Coerce a JSON value into a [0, 1] float, or None when nonsensical."""
    try:
        score = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, score))


def parse_verdict(text: str) -> Verdict:
    """Robust JSON extraction supporting both old (single-score) and new (multi-dim) shapes.

    The new shape is `{"overall": x, "scores": {...}, "explanations": {...}, "reason": "..."}`.
    The old shape `{"score": x, "reason": "..."}` is still accepted — Anthill
    used to ship that one and a judge that hasn't been re-prompted yet will
    keep returning it. We treat the legacy single score as `overall` with
    no per-dimension breakdown.

    Any failure (no JSON / bad types / empty payload) falls back to a
    neutral 0.5 verdict rather than crashing — a misbehaving judge is
    its own bug, not the worker's.
    """
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, flags=re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return Verdict(score=0.5, reason="judge returned unparseable text")
        else:
            return Verdict(score=0.5, reason="judge returned no JSON")

    if not isinstance(data, dict):
        return Verdict(score=0.5, reason="judge returned non-object JSON")

    # Per-dimension scores: normalize keys via the same path that the
    # DimensionCatalog uses, so judge wording variance doesn't fragment trails.
    raw_scores = data.get("scores") if isinstance(data.get("scores"), dict) else {}
    scores: dict[str, float] = {}
    for k, v in raw_scores.items():
        if not isinstance(k, str):
            continue
        key = normalize_dim(k)
        if not key:
            continue
        coerced = _coerce_score(v)
        if coerced is None:
            continue
        scores[key] = coerced

    raw_expl = data.get("explanations") if isinstance(data.get("explanations"), dict) else {}
    explanations: dict[str, str] = {}
    for k, v in raw_expl.items():
        if not isinstance(k, str):
            continue
        key = normalize_dim(k)
        if not key:
            continue
        explanations[key] = str(v).strip()

    # Overall: prefer explicit field, fall back to legacy "score", fall back
    # to mean of dimension scores, fall back to 0.5.
    overall = _coerce_score(data.get("overall"))
    if overall is None:
        overall = _coerce_score(data.get("score"))
    if overall is None and scores:
        overall = sum(scores.values()) / len(scores)
    if overall is None:
        overall = 0.5

    reason = str(data.get("reason", "")).strip() or "no reason given"
    return Verdict(score=overall, reason=reason, scores=scores, explanations=explanations)


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
            max_tokens=400,  # room for multi-dim breakdown + explanations
        )
    except Exception as e:  # noqa: BLE001
        return Verdict(score=0.5, reason=f"judge unavailable: {e}")
    return parse_verdict(response.text)
