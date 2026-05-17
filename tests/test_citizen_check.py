"""0.1.21 — citizen-model preflight + auth-error classification.

Real bug: a citizen has model="minimax" but the user's UserConfig
only has a ModelEntry named "deepseek". get_provider falls through
to the legacy MiniMaxProvider with no key, MiniMax returns "error
1004: login fail", every ask burns 3 retries with "(unknown)".

This patch:
- Adds FailureReason.AUTH so the retry log says "(auth)" not "(unknown)"
- Adds find_unresolvable_citizens / migrate_citizens_to in
  core/citizen_check.py
- Adds a startup preflight warning + /citizens migrate slash command
  (the slash command is tested via the underlying functions; the
  REPL string layer is integration-tested manually)
"""

from __future__ import annotations

from anthill.core.agent import Agent


# --- failure classification ------------------------------------------------


def test_minimax_1004_classifies_as_auth() -> None:
    from anthill.core.failure import FailureReason, classify_attempt

    msg = (
        "MiniMax error 1004: login fail: Please carry the API secret "
        "key in the 'Authorization' field"
    )
    assert classify_attempt(msg) == FailureReason.AUTH


def test_openai_401_classifies_as_auth() -> None:
    from anthill.core.failure import FailureReason, classify_attempt

    assert classify_attempt("401 Unauthorized") == FailureReason.AUTH
    assert classify_attempt("Incorrect API key provided: sk-...") == FailureReason.AUTH


def test_anthropic_invalid_key_classifies_as_auth() -> None:
    from anthill.core.failure import FailureReason, classify_attempt

    assert (
        classify_attempt('{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}')
        == FailureReason.AUTH
    )


def test_auth_explain_mentions_remedy() -> None:
    """The human-readable explain string for AUTH should hint at fix."""
    from anthill.core.failure import FailureReason, explain

    text = explain(FailureReason.AUTH)
    # Just needs to not be the generic "could not be classified" one.
    assert "key" in text.lower() or "auth" in text.lower()


def test_auth_priority_over_rate_limit() -> None:
    """A 401 message that also mentions 'limit' should still bucket AUTH."""
    from anthill.core.failure import FailureReason, classify_attempt

    msg = "401 Unauthorized: API rate limit exceeded for this token"
    assert classify_attempt(msg) == FailureReason.AUTH


# --- citizen_check.find_unresolvable_citizens -----------------------------


def test_no_unresolvable_when_all_models_configured() -> None:
    from anthill.core.citizen_check import find_unresolvable_citizens

    agents = [Agent(id="ant-1", model="deepseek"), Agent(id="ant-2", model="deepseek")]
    issues = find_unresolvable_citizens(agents, ["deepseek"])
    assert issues == []


def test_unresolvable_when_model_name_missing(monkeypatch) -> None:
    """Citizen says 'minimax' but no ModelEntry + no MINIMAX_API_KEY."""
    from anthill.core.citizen_check import find_unresolvable_citizens

    # Ensure none of the legacy env vars are set.
    for v in ("ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(v, raising=False)

    agents = [Agent(id="ant-1", model="minimax")]
    issues = find_unresolvable_citizens(agents, ["deepseek"])
    assert len(issues) == 1
    assert issues[0].model == "minimax"
    assert issues[0].agent_id == "ant-1"


def test_legacy_env_var_no_longer_saves_when_user_config_exists(monkeypatch) -> None:
    """0.1.22: when UserConfig is in play, env-var fallback is a stale
    signal, not a clean resolve. Real-world case: user has
    MINIMAX_API_KEY exported from earlier testing but now uses the
    'deepseek' ModelEntry — citizens stuck on 'minimax' would still
    hit auth errors via the legacy provider."""
    from anthill.core.citizen_check import find_unresolvable_citizens

    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")
    agents = [Agent(id="ant-1", model="minimax")]
    issues = find_unresolvable_citizens(agents, ["deepseek"])
    assert len(issues) == 1
    assert issues[0].reason == "stale_legacy"


def test_legacy_env_var_still_resolves_when_no_user_config(monkeypatch) -> None:
    """Heritage env-driven mode (no UserConfig at all) keeps working —
    we don't break users who never ran `anthill setup`."""
    from anthill.core.citizen_check import find_unresolvable_citizens

    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")
    agents = [Agent(id="ant-1", model="minimax")]
    # configured_model_names is empty => env-var path is the source of truth
    issues = find_unresolvable_citizens(agents, [])
    assert issues == []


def test_no_user_config_no_env_still_flagged(monkeypatch) -> None:
    """No config AND no env var → no_match (the original case)."""
    from anthill.core.citizen_check import find_unresolvable_citizens

    for v in ("ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    agents = [Agent(id="ant-1", model="minimax")]
    issues = find_unresolvable_citizens(agents, [])
    assert len(issues) == 1
    assert issues[0].reason == "no_match"


def test_retired_and_quarantined_skipped(monkeypatch) -> None:
    """Retired / quarantined citizens don't trigger warnings."""
    import time

    from anthill.core.citizen_check import find_unresolvable_citizens

    for v in ("ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(v, raising=False)

    a1 = Agent(id="ant-1", model="minimax", retired_at=time.time())
    a2 = Agent(id="ant-2", model="minimax", quarantined_at=time.time())
    issues = find_unresolvable_citizens([a1, a2], ["deepseek"])
    assert issues == []


# --- citizen_check.migrate_citizens_to ------------------------------------


def test_migrate_only_unresolvable(monkeypatch) -> None:
    """Default behavior: leave good citizens alone, fix broken ones."""
    from anthill.core.citizen_check import migrate_citizens_to

    for v in ("ANTHILL_MINIMAX_KEY", "MINIMAX_API_KEY"):
        monkeypatch.delenv(v, raising=False)

    agents = [
        Agent(id="ant-1", model="minimax"),     # broken
        Agent(id="ant-2", model="deepseek"),    # good
        Agent(id="ant-3", model="minimax"),     # broken
    ]
    n = migrate_citizens_to(
        agents, "deepseek",
        only_unresolvable=True,
        configured_model_names=["deepseek"],
    )
    assert n == 2
    assert [a.model for a in agents] == ["deepseek", "deepseek", "deepseek"]


def test_migrate_all_blasts_everyone() -> None:
    """only_unresolvable=False repoints every alive citizen."""
    from anthill.core.citizen_check import migrate_citizens_to

    agents = [
        Agent(id="ant-1", model="minimax"),
        Agent(id="ant-2", model="deepseek"),
        Agent(id="ant-3", model="something-else"),
    ]
    n = migrate_citizens_to(agents, "deepseek", only_unresolvable=False)
    assert n == 2  # ant-2 was already deepseek; only ant-1 + ant-3 changed
    assert all(a.model == "deepseek" for a in agents)


def test_migrate_skips_retired_quarantined() -> None:
    """Retired / quarantined citizens stay on whatever they were."""
    import time

    from anthill.core.citizen_check import migrate_citizens_to

    a1 = Agent(id="ant-1", model="minimax", retired_at=time.time())
    a2 = Agent(id="ant-2", model="minimax", quarantined_at=time.time())
    a3 = Agent(id="ant-3", model="minimax")
    n = migrate_citizens_to(
        [a1, a2, a3], "deepseek", only_unresolvable=False,
    )
    assert n == 1
    assert a1.model == "minimax"  # retired, untouched
    assert a2.model == "minimax"  # quarantined, untouched
    assert a3.model == "deepseek"  # alive, migrated


def test_migrate_repairs_stale_legacy_when_user_config_exists(monkeypatch) -> None:
    """0.1.23 regression: the exact user-reported case.

    User has UserConfig with one ModelEntry "deepseek" and three
    citizens stuck on "minimax". Their shell still has MINIMAX_API_KEY
    exported (stale). In 0.1.22 find_unresolvable_citizens correctly
    flagged the three, but migrate_citizens_to silently skipped them
    because it kept the old _legacy_resolves check inline.
    """
    from anthill.core.citizen_check import migrate_citizens_to

    monkeypatch.setenv("MINIMAX_API_KEY", "sk-stale-from-yesterday")

    agents = [
        Agent(id="ant-1", model="minimax"),
        Agent(id="ant-2", model="minimax"),
        Agent(id="ant-3", model="minimax"),
    ]
    n = migrate_citizens_to(
        agents, "deepseek",
        only_unresolvable=True,
        configured_model_names=["deepseek"],
    )
    assert n == 3
    assert all(a.model == "deepseek" for a in agents)


def test_migrate_leaves_heritage_env_only_users_alone(monkeypatch) -> None:
    """Mirror: with NO UserConfig, env-driven heritage users are not
    forcibly migrated. The fix should not regress them."""
    from anthill.core.citizen_check import migrate_citizens_to

    monkeypatch.setenv("MINIMAX_API_KEY", "sk-real")
    agents = [Agent(id="ant-1", model="minimax")]
    n = migrate_citizens_to(
        agents, "deepseek",
        only_unresolvable=True,
        configured_model_names=[],   # heritage mode
    )
    assert n == 0
    assert agents[0].model == "minimax"


def test_migrate_from_specific_model() -> None:
    """0.1.24: the "third case" — model is configured but key is bad.
    User runs `/citizens migrate minimax` to move off it explicitly.
    """
    from anthill.core.citizen_check import migrate_citizens_from

    agents = [
        Agent(id="ant-1", model="minimax"),
        Agent(id="ant-2", model="deepseek"),
        Agent(id="ant-3", model="minimax"),
    ]
    n = migrate_citizens_from(agents, from_model="minimax", to_model="deepseek")
    assert n == 2
    assert [a.model for a in agents] == ["deepseek", "deepseek", "deepseek"]


def test_migrate_from_skips_unrelated_citizens() -> None:
    """Citizens on other models stay put — migrate_from is scoped."""
    from anthill.core.citizen_check import migrate_citizens_from

    agents = [
        Agent(id="ant-1", model="minimax"),
        Agent(id="ant-2", model="openai"),  # configured but unrelated
    ]
    n = migrate_citizens_from(agents, from_model="minimax", to_model="deepseek")
    assert n == 1
    assert agents[1].model == "openai"


def test_migrate_from_skips_retired_quarantined() -> None:
    """Retired / quarantined citizens stay on their model."""
    import time

    from anthill.core.citizen_check import migrate_citizens_from

    a1 = Agent(id="ant-1", model="minimax", retired_at=time.time())
    a2 = Agent(id="ant-2", model="minimax", quarantined_at=time.time())
    a3 = Agent(id="ant-3", model="minimax")
    n = migrate_citizens_from([a1, a2, a3], from_model="minimax", to_model="deepseek")
    assert n == 1
    assert a1.model == "minimax"
    assert a2.model == "minimax"
    assert a3.model == "deepseek"


def test_migrate_from_to_same_is_noop() -> None:
    from anthill.core.citizen_check import migrate_citizens_from

    agents = [Agent(id="ant-1", model="minimax")]
    n = migrate_citizens_from(agents, from_model="minimax", to_model="minimax")
    assert n == 0
    assert agents[0].model == "minimax"


def test_migrate_from_clears_provider_cache() -> None:
    from anthill.core.citizen_check import migrate_citizens_from

    a = Agent(id="ant-1", model="minimax")
    a._provider = "fake-cached"  # type: ignore[assignment]
    migrate_citizens_from([a], from_model="minimax", to_model="deepseek")
    assert a._provider is None


def test_migrate_clears_provider_cache(monkeypatch) -> None:
    """After migration, the lazy provider cache must rebuild — else the
    citizen keeps using the old provider for the rest of the session."""
    from anthill.core.citizen_check import migrate_citizens_to

    a = Agent(id="ant-1", model="minimax")
    a._provider = "fake-cached-provider"  # type: ignore[assignment]
    migrate_citizens_to([a], "deepseek", only_unresolvable=False)
    assert a._provider is None
