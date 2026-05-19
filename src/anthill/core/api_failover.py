"""0.1.59 — provider-aware retry forbid expansion.

Hermes has `agent/error_classifier.py` that maps API errors to a
`FailoverReason` and automatically retries against a *different
provider* (not just a different request to the same one). anthill
has the classification half already (`core/failure.py` —
RATE_LIMIT / NETWORK / TIMEOUT / AUTH / MODEL_ERROR) but the retry
path only knows to forbid the failing AGENT, not all agents that
share the same MODEL.

This module closes that gap. When a retry follows a transient API
failure (the provider itself is unhappy, not the prompt), the
forbid set expands to cover every agent that runs on the same model
— forcing the next attempt onto a different provider IF the nation
has one configured.

If the nation has only one model, we don't artificially block the
retry (we'd just fail with "no agents available"); the old single-
agent retry still runs.

Design notes:
  - This is a forbid EXPANSION, not a replacement. Caller still owns
    the set; we just add to it.
  - Decisions are PURE (no side effects). Caller does the actual
    re-route via `nation.run(..., forbid=...)`.
  - We deliberately do NOT failover on AUTH errors — those are config
    bugs, switching providers just hides them.
"""

from __future__ import annotations

from typing import Iterable

# Failure reasons where the next attempt should prefer a *different
# provider* (model), not just a different citizen. Keep tight: false
# positives here cause unnecessary provider rotation and erode
# pheromone learning signal.
TRANSIENT_API_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "rate_limit",       # 429 — try another provider
        "network",          # 5xx / connection — try another provider
        "timeout",          # provider slow / hung — try another provider
        "model_error",      # generic 4xx — sometimes provider-specific
    }
)

# Reasons where provider-failover would HIDE the real bug. Do not
# trigger expansion on these.
NON_FAILOVER_REASONS: frozenset[str] = frozenset(
    {
        "auth",                  # bad key — switching providers won't help
        "policy_refusal",        # different model might also refuse
        "user_serving_refusal",  # 0.1.40 retry-with-nudge owns this
        "format_error",          # Scout JSON bug, model-agnostic
        "judge_low",             # quality issue, not API issue
        "empty_response",        # might be model-specific but ambiguous;
                                 # let citizen rotation handle it
        "truncated",             # max_tokens config issue
        "unknown",               # don't speculate
    }
)


def should_failover_provider(failure_reason: str | None) -> bool:
    """True when the next retry should prefer a different provider.

    Conservative default: unknown / None → False. Only the
    explicitly-listed transient API failures opt in.
    """
    if failure_reason is None:
        return False
    return failure_reason in TRANSIENT_API_FAILURE_REASONS


def expand_forbid_for_failover(
    forbid: set[str],
    failed_agent_model: str | None,
    *,
    all_agents: Iterable[tuple[str, str]],
    nation_models: int,
) -> set[str]:
    """Add to ``forbid`` every agent_id sharing ``failed_agent_model``.

    ``all_agents`` yields (agent_id, model_name) for every citizen in
    the nation. ``nation_models`` is the count of DISTINCT models —
    used to bail when there's only one (forbidding everyone deadlocks
    the retry).

    Returns a NEW set; doesn't mutate the input. Caller decides whether
    to pass the expanded set into the next retry.
    """
    if failed_agent_model is None:
        return set(forbid)
    if nation_models <= 1:
        # Only one model available — failing over would block everyone.
        # Keep the original forbid; let citizen rotation handle it.
        return set(forbid)
    expanded = set(forbid)
    for agent_id, model in all_agents:
        if model == failed_agent_model:
            expanded.add(agent_id)
    return expanded
