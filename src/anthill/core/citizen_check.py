"""0.1.21 — validate that every citizen's ``model`` field actually resolves.

The bug we hit: a citizen had ``model="minimax"`` left over from an
earlier session, but the user's current ``UserConfig`` only had a
ModelEntry named ``"deepseek"``. ``get_provider("minimax")`` fell
through to the legacy ``_REGISTRY`` which built ``MiniMaxProvider()``
with no key, and every ask died on auth.

This module:

- Walks alive citizens and asks "does this model name resolve to a
  user-configured ModelEntry, OR a legacy registry alias that we
  can actually reach (i.e. its env vars are present)?"
- For each broken citizen, records the gap so the REPL can warn at
  startup AND surface a one-line `/citizens migrate` action.
- Pure-stdlib + cheap: just dict lookups + env checks, no network.

Migration is a separate function (``migrate_citizens_to``) so the
diagnostic and the fix stay testable apart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from anthill.core.agent import Agent


# Models in the legacy registry that still work via env vars. Mirrors
# the keys of anthill.models.registry._REGISTRY; we accept these as
# "resolves cleanly" only when the matching env var is set.
_LEGACY_MODEL_ENV: dict[str, tuple[str, ...]] = {
    "deepseek": ("ANTHILL_DEEPSEEK_KEY", "DEEPSEEK_API_KEY"),
    "deepseek-chat": ("ANTHILL_DEEPSEEK_KEY", "DEEPSEEK_API_KEY"),
    "deepseek-reasoner": ("ANTHILL_DEEPSEEK_KEY", "DEEPSEEK_API_KEY"),
    "minimax": ("ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY"),
    "minimax-m2-stable": ("ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY"),
    "minimax-m2": ("ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY"),
    "minimax-m2.5": ("ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY"),
}


@dataclass(frozen=True)
class CitizenIssue:
    """One unresolvable citizen ⇒ one CitizenIssue.

    ``reason`` is a short token the REPL can render distinctly from
    a human label. Today we surface only ``"no_match"`` (no
    ModelEntry, no env), but the type stays open for future
    refinements (e.g. quarantine-only, model-id retired upstream).
    """

    agent_id: str
    model: str
    reason: str  # short token; "no_match" today


def _legacy_resolves(model: str) -> bool:
    """True when a legacy alias has the env var its constructor needs."""
    env_vars = _LEGACY_MODEL_ENV.get(model)
    if env_vars is None:
        return False
    return any(os.environ.get(v) for v in env_vars)


def find_unresolvable_citizens(
    agents: Iterable[Agent],
    configured_model_names: Iterable[str],
) -> list[CitizenIssue]:
    """Citizens whose ``model`` matches neither a configured entry
    nor a legacy alias with env vars present.

    Retired / quarantined citizens are skipped — they don't get
    assigned new work, so an unresolvable model on them isn't
    user-visible.
    """
    configured = set(configured_model_names)
    issues: list[CitizenIssue] = []
    for agent in agents:
        if agent.is_retired or agent.is_quarantined:
            continue
        if agent.model in configured:
            continue
        if _legacy_resolves(agent.model):
            continue
        issues.append(
            CitizenIssue(agent_id=agent.id, model=agent.model, reason="no_match")
        )
    return issues


def migrate_citizens_to(
    agents: Iterable[Agent],
    target_model: str,
    *,
    only_unresolvable: bool = True,
    configured_model_names: Iterable[str] | None = None,
) -> int:
    """Point citizens at ``target_model``. Returns count changed.

    ``only_unresolvable=True`` (default) touches only citizens whose
    current model fails ``find_unresolvable_citizens`` — preserves
    citizens the user deliberately put on a different model.

    ``only_unresolvable=False`` is the "blast everyone" option, useful
    when the user wants to consolidate after experimenting.

    Retired / quarantined citizens are NEVER touched — leaving them
    on whatever they were is the lower-surprise default.
    """
    configured = set(configured_model_names or ())
    n = 0
    for agent in agents:
        if agent.is_retired or agent.is_quarantined:
            continue
        if agent.model == target_model:
            continue
        if only_unresolvable:
            if agent.model in configured:
                continue
            if _legacy_resolves(agent.model):
                continue
        agent.model = target_model
        # Force the lazy provider cache to rebuild on next execute()
        # — without this, the agent would keep its prior provider
        # instance for the lifetime of the process.
        agent._provider = None
        n += 1
    return n
