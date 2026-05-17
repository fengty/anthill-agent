"""Deliberation loop — keep refining until quality crosses the threshold.

Other agent frameworks (Hermes-style) loop a single agent until it says
"done" — the agent grades its own progress. That's fast but biased:
self-evaluation tends to call things finished too early. Anthill's
deliberation does it differently:

  1. Round 1: Scout decomposes + nation executes (normal `ask` path)
  2. Score the output across whatever quality dimensions the judge / user
     have observed (v0.4 DimensionCatalog)
  3. If quality < threshold: spawn a *different* citizen as a critic,
     ask it to find specific weaknesses by dimension
  4. Round 2: refined request = original + critique → re-execute,
     letting the router pick whoever's strongest given the new weights
  5. Repeat until quality met, max_rounds hit, or no improvement

What makes this richer than Hermes-style auto-loop:
  - **Stop signal is objective.** A judge other than the writer scores
    the work; we don't trust the writer to say "good enough."
  - **Different citizens at different stages.** Author / critic / reviser
    can be different models — the router automatically picks who's
    strongest on this dimension's pheromone trail.
  - **No silent stagnation.** A round that doesn't improve quality
    enough triggers stop; the user sees why.
  - **Budget + quality + iteration limits compose.** Whichever hits
    first wins — no surprise overspend, no surprise stagnation.

This module owns only the *orchestration*: when to critique, when to
stop. Per-round execution remains the normal Nation.ask code path so
every existing loop (pheromone, judge, dimensions, immune, fanout,
inflight) still applies inside each round.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from anthill.core.budget import Budget
    from anthill.core.nation import AskResult, Nation


# Defaults — conservative. Most asks should NOT need 3 rounds; we cap to
# avoid runaway cost, and threshold is realistic (judge LLM scores
# rarely hit 0.95+ without genuine quality).
DEFAULT_MAX_ROUNDS = 3
DEFAULT_QUALITY_THRESHOLD = 0.85
DEFAULT_MIN_IMPROVEMENT = 0.03  # round-over-round; below this is "diverged"


# Why a deliberation stopped — used by UI to explain "we stopped here because..."
StopReason = str  # one of: "quality_met", "max_rounds", "budget", "stagnated", "first_round_fine"


@dataclass
class DeliberationRound:
    """One pass of the loop — the request, the result, the quality."""

    round_num: int                 # 1-indexed
    request: str                   # the prompt sent THIS round (refined)
    ask_result: "AskResult"        # full AskResult of this round
    quality: float                 # overall [0, 1] for this round
    quality_by_dim: dict[str, float] = field(default_factory=dict)
    critique: str | None = None    # critique that produced THIS round (None for round 1)
    critique_by: str | None = None # which citizen produced the critique


@dataclass
class DeliberationResult:
    """All rounds + final verdict."""

    rounds: list[DeliberationRound]
    final_round: DeliberationRound  # the winning round (highest quality, OR last one)
    stop_reason: StopReason
    converged: bool                # True iff stop_reason == "quality_met"

    @property
    def final_output(self) -> str:
        return self.final_round.ask_result.final_output

    @property
    def total_rounds(self) -> int:
        return len(self.rounds)

    @property
    def quality_trajectory(self) -> list[float]:
        return [r.quality for r in self.rounds]


# --- quality computation -------------------------------------------------


def _quality_of(result: "AskResult") -> tuple[float, dict[str, float]]:
    """Distill an AskResult into an overall quality + per-dimension breakdown.

    Strategy:
      - Walk every attempt's `scores` dict (filled by judge in v0.4)
      - Per dimension, take the MAX score across attempts in this ask
        (we care that the nation *can* produce X, not the average flop)
      - Overall = mean of per-dimension scores; if no dimensions
        recorded, fall back to mean of attempt-level success_score.

    Returns (0.0, {}) for empty result — defensively.
    """
    if not result.outcomes:
        return 0.0, {}

    # 0.1.26 — truncation override. If any successful outcome's
    # winning attempt was truncated (stopped on max_tokens), cap the
    # overall quality at 0.6 regardless of judge dimension scores.
    # A mid-sentence answer can't be 100%. This forces the
    # deliberation loop to go another round instead of declaring
    # "first_round_fine" on a half-answer.
    truncated_any = False

    per_dim_max: dict[str, float] = {}
    success_scores: list[float] = []
    for outcome in result.outcomes:
        if outcome.status != "ok":
            continue
        # getattr keeps the duck-typed test fixtures (_FakeOutcome
        # without a .final field) working — defaults to None which
        # leaves truncated_any False.
        winner = getattr(outcome, "final", None)
        if winner is not None and getattr(winner, "truncated", False):
            truncated_any = True
        for attempt in outcome.attempts:
            success_scores.append(float(attempt.success_score))
            scores = getattr(attempt, "scores", None) or {}
            for dim, val in scores.items():
                v = max(0.0, min(1.0, float(val)))
                if v > per_dim_max.get(dim, -1.0):
                    per_dim_max[dim] = v

    if per_dim_max:
        # Don't double-count the 'cost' dim — it's an efficiency proxy,
        # not a quality signal. Leaving it in would make a cheap-but-bad
        # answer score high.
        quality_dims = {k: v for k, v in per_dim_max.items() if k != "cost"}
        if quality_dims:
            overall = sum(quality_dims.values()) / len(quality_dims)
        else:
            overall = per_dim_max.get("cost", 0.0)
    elif success_scores:
        overall = sum(success_scores) / len(success_scores)
    else:
        overall = 0.0

    # Truncation cap, see comment at top of function.
    if truncated_any and overall > 0.6:
        overall = 0.6

    return max(0.0, min(1.0, overall)), per_dim_max


# --- critique step --------------------------------------------------------


_CRITIQUE_PROMPT_TEMPLATE = """The following answer was produced for a user request.
Your job is to act as a strict reviewer and point out concrete weaknesses.

USER REQUEST:
{request}

ANSWER UNDER REVIEW:
{answer}

Identified weak dimensions (from the nation's quality model):
{weak_dims}

Write a critique in 3-6 bullet points. Be concrete: name what is missing,
inaccurate, unclear, or off-tone. Do NOT rewrite the answer. Do NOT pad
with praise. Each bullet should be one short sentence the next reviser
can act on directly.

Output the bullets only — no preamble."""


async def _make_critique(
    nation: "Nation",
    request: str,
    answer: str,
    weak_dims: dict[str, float],
) -> tuple[str, str]:
    """Ask a citizen to critique the latest output. Returns (text, agent_id).

    The critique task_type is 'review' so the router can develop a
    distinct pheromone trail for reviewing vs. writing. The dim_weights
    are inverted on the weak axes so the router picks a citizen
    historically strong on what the answer is currently weakest at.
    """
    weak_summary = "\n".join(
        f"  - {dim}: {score:.2f}" for dim, score in sorted(weak_dims.items(), key=lambda kv: kv[1])
    ) or "  (no dimensions yet recorded — find anything that's weak)"

    prompt = _CRITIQUE_PROMPT_TEMPLATE.format(
        request=request,
        answer=answer,
        weak_dims=weak_summary,
    )
    # Critique is its own task_type. The router will route based on
    # whoever scored well at "review" historically; cold-start picks
    # a random citizen.
    result = await nation.run("review", prompt)
    return str(result.output).strip(), result.agent_id


# --- refine step ---------------------------------------------------------


_REFINE_REQUEST_TEMPLATE = """{original_request}

---
DRAFT (from a previous round of attention):
{prev_answer}

CRITIQUE (from a peer reviewer — address these specifically):
{critique}

Produce a revised version that fixes the issues. Keep what worked.
Do not just restate the critique — rewrite the answer."""


def _build_refine_request(
    original_request: str,
    prev_answer: str,
    critique: str,
) -> str:
    return _REFINE_REQUEST_TEMPLATE.format(
        original_request=original_request,
        prev_answer=prev_answer,
        critique=critique,
    )


# --- the loop ------------------------------------------------------------


# A small progress hook so the REPL/CLI can render each round live.
RoundCallback = Callable[[DeliberationRound], Awaitable[None]]


async def deliberate(
    nation: "Nation",
    request: str,
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    quality_threshold: float = DEFAULT_QUALITY_THRESHOLD,
    min_improvement: float = DEFAULT_MIN_IMPROVEMENT,
    budget: "Budget | None" = None,
    on_round: RoundCallback | None = None,
    **ask_kwargs,
) -> DeliberationResult:
    """Run the multi-round refinement loop. Returns full transcript + winner.

    `ask_kwargs` is forwarded to Nation.ask for each round (so the
    caller can pass on_progress, nation_dir, max_replans, etc).

    Stops on first of:
      - quality_threshold reached
      - max_rounds rounds executed
      - budget exhausted (per Nation.ask's budget tracker)
      - no improvement (round-over-round delta < min_improvement) AND
        we already have >= 2 rounds
    """
    rounds: list[DeliberationRound] = []

    # ROUND 1 — normal ask
    result = await nation.ask(request, budget=budget, **ask_kwargs)
    q, by_dim = _quality_of(result)
    rounds.append(DeliberationRound(
        round_num=1, request=request, ask_result=result,
        quality=q, quality_by_dim=by_dim,
    ))
    if on_round is not None:
        await on_round(rounds[-1])

    # v0.8.1: short-circuit on trivial complexity. A trivial request
    # (greeting, single-word ack, simple factual) should not be put
    # through the critique-and-refine wringer no matter what the
    # judge scored — the loop assumes the work has SHAPE to improve,
    # which trivial output doesn't.
    plan_complexity = getattr(result.plan, "complexity", "normal")
    if plan_complexity == "trivial":
        return DeliberationResult(
            rounds=rounds,
            final_round=rounds[-1],
            stop_reason="trivial",
            converged=True,
        )

    if q >= quality_threshold:
        return DeliberationResult(
            rounds=rounds,
            final_round=rounds[-1],
            stop_reason="first_round_fine",
            converged=True,
        )

    if budget is not None and result.budget is not None and result.budget.exhausted:
        return DeliberationResult(
            rounds=rounds,
            final_round=rounds[-1],
            stop_reason="budget",
            converged=False,
        )

    # ROUND 2..N — critique + refine
    for round_num in range(2, max_rounds + 1):
        prev = rounds[-1]

        # Pick the dimensions where the prev answer is weakest, biased
        # toward dimensions that the catalog has weight on (i.e. the
        # ones the user actually cares about).
        weakest = dict(sorted(prev.quality_by_dim.items(), key=lambda kv: kv[1])[:3])

        try:
            critique, critic_id = await _make_critique(
                nation,
                request=request,
                answer=prev.ask_result.final_output,
                weak_dims=weakest,
            )
        except Exception as e:  # noqa: BLE001 — critique is best-effort
            critique = f"[critique unavailable: {e}]"
            critic_id = None

        refined_request = _build_refine_request(
            request, prev.ask_result.final_output, critique
        )
        result = await nation.ask(refined_request, budget=budget, **ask_kwargs)
        q, by_dim = _quality_of(result)
        this_round = DeliberationRound(
            round_num=round_num,
            request=refined_request,
            ask_result=result,
            quality=q,
            quality_by_dim=by_dim,
            critique=critique,
            critique_by=critic_id,
        )
        rounds.append(this_round)
        if on_round is not None:
            await on_round(this_round)

        if q >= quality_threshold:
            return DeliberationResult(
                rounds=rounds,
                final_round=this_round,
                stop_reason="quality_met",
                converged=True,
            )
        if budget is not None and result.budget is not None and result.budget.exhausted:
            return DeliberationResult(
                rounds=rounds,
                final_round=_best_round(rounds),
                stop_reason="budget",
                converged=False,
            )
        if q < prev.quality - min_improvement * 2:
            # Got noticeably worse — stop and use the best round we have.
            return DeliberationResult(
                rounds=rounds,
                final_round=_best_round(rounds),
                stop_reason="stagnated",
                converged=False,
            )
        if abs(q - prev.quality) < min_improvement and round_num >= 2:
            # Plateaued: not better, not worse. Stop early.
            return DeliberationResult(
                rounds=rounds,
                final_round=_best_round(rounds),
                stop_reason="stagnated",
                converged=False,
            )

    return DeliberationResult(
        rounds=rounds,
        final_round=_best_round(rounds),
        stop_reason="max_rounds",
        converged=False,
    )


def _best_round(rounds: list[DeliberationRound]) -> DeliberationRound:
    """Pick the highest-quality round; ties break to the latest."""
    return max(rounds, key=lambda r: (r.quality, r.round_num))


__all__ = [
    "DeliberationRound",
    "DeliberationResult",
    "deliberate",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_QUALITY_THRESHOLD",
    "DEFAULT_MIN_IMPROVEMENT",
]
