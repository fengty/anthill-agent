"""0.1.59 — provider-aware retry forbid expansion.

Verifies:
  - should_failover_provider: opt-in semantics per FailureReason
  - expand_forbid_for_failover: forbids same-model agents
  - single-model nation: NO expansion (would deadlock retries)
  - None failed_model: pass-through (defensive)
  - immutability: input set is not mutated
  - auth / policy / user_serving_refusal don't trigger expansion
    (those are "switching providers hides the bug" cases)
"""

from __future__ import annotations

from anthill.core.api_failover import (
    NON_FAILOVER_REASONS,
    TRANSIENT_API_FAILURE_REASONS,
    expand_forbid_for_failover,
    should_failover_provider,
)


# --- should_failover_provider --------------------------------------------


def test_should_failover_on_rate_limit() -> None:
    assert should_failover_provider("rate_limit") is True


def test_should_failover_on_network() -> None:
    assert should_failover_provider("network") is True


def test_should_failover_on_timeout() -> None:
    assert should_failover_provider("timeout") is True


def test_should_failover_on_model_error() -> None:
    # Generic 4xx is sometimes provider-specific (e.g. malformed
    # request body that one provider rejects but another accepts).
    assert should_failover_provider("model_error") is True


def test_should_NOT_failover_on_auth() -> None:
    """Bad API key — switching providers hides the config bug."""
    assert should_failover_provider("auth") is False


def test_should_NOT_failover_on_user_serving_refusal() -> None:
    """The 0.1.40 retry path owns this — don't double-handle."""
    assert should_failover_provider("user_serving_refusal") is False


def test_should_NOT_failover_on_policy_refusal() -> None:
    """A different provider would likely also refuse. Don't try."""
    assert should_failover_provider("policy_refusal") is False


def test_should_NOT_failover_on_none() -> None:
    """Defensive: None / unknown → don't force a provider switch."""
    assert should_failover_provider(None) is False
    assert should_failover_provider("unknown") is False


def test_failover_reasons_disjoint_from_non_failover() -> None:
    """Sanity: a failure reason can't be in BOTH lists."""
    assert not (TRANSIENT_API_FAILURE_REASONS & NON_FAILOVER_REASONS)


# --- expand_forbid_for_failover ------------------------------------------


def test_expand_forbids_same_model_agents() -> None:
    """When deepseek/agent-1 fails with rate_limit, forbid every
    deepseek-backed citizen so the retry definitely lands on minimax."""
    forbid = {"ant-deepseek-1"}
    expanded = expand_forbid_for_failover(
        forbid,
        failed_agent_model="deepseek",
        all_agents=[
            ("ant-deepseek-1", "deepseek"),
            ("ant-deepseek-2", "deepseek"),
            ("ant-minimax-1", "minimax"),
        ],
        nation_models=2,
    )
    assert expanded == {"ant-deepseek-1", "ant-deepseek-2"}
    # Minimax citizen is still allowed.
    assert "ant-minimax-1" not in expanded


def test_expand_skips_when_only_one_model() -> None:
    """Nation has only one model — expanding would forbid everyone
    and deadlock the retry. Keep the original forbid set."""
    forbid = {"ant-deepseek-1"}
    expanded = expand_forbid_for_failover(
        forbid,
        failed_agent_model="deepseek",
        all_agents=[
            ("ant-deepseek-1", "deepseek"),
            ("ant-deepseek-2", "deepseek"),
        ],
        nation_models=1,
    )
    assert expanded == {"ant-deepseek-1"}


def test_expand_returns_new_set_does_not_mutate_input() -> None:
    forbid = {"ant-deepseek-1"}
    original = set(forbid)
    _ = expand_forbid_for_failover(
        forbid,
        failed_agent_model="deepseek",
        all_agents=[
            ("ant-deepseek-1", "deepseek"),
            ("ant-deepseek-2", "deepseek"),
            ("ant-minimax-1", "minimax"),
        ],
        nation_models=2,
    )
    assert forbid == original  # input unchanged


def test_expand_pass_through_when_failed_model_unknown() -> None:
    """Defensive: if we couldn't resolve the failing agent's model,
    don't expand — fall back to citizen rotation alone."""
    forbid = {"ant-x"}
    expanded = expand_forbid_for_failover(
        forbid,
        failed_agent_model=None,
        all_agents=[("ant-x", "deepseek"), ("ant-y", "minimax")],
        nation_models=2,
    )
    assert expanded == {"ant-x"}


def test_expand_works_with_empty_all_agents() -> None:
    expanded = expand_forbid_for_failover(
        set(), failed_agent_model="deepseek", all_agents=[], nation_models=2
    )
    assert expanded == set()
