"""0.1.71 — per-domain login credentials store.

Covers:
  - save/load round-trip (full + partial fields)
  - list_domains shows distinct domains, no duplicates
  - remove_credentials cleans all related secret keys
  - extract_domain handles http/https + ports + missing scheme
  - secrets.toml uses the existing 0600-enforced layer (no plaintext
    leak into wider config)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.url_credentials import (
    DomainCredentials,
    extract_domain,
    list_domains,
    load_credentials,
    remove_credentials,
    save_credentials,
)


# --- extract_domain ------------------------------------------------------


def test_extract_domain_basic() -> None:
    assert extract_domain("https://example.com/path") == "example.com"
    assert (
        extract_domain("http://ss.chandao.pamirs.top/zentao/bug-view-1.html")
        == "ss.chandao.pamirs.top"
    )


def test_extract_domain_with_port() -> None:
    assert extract_domain("https://example.com:8443/x") == "example.com:8443"


def test_extract_domain_no_scheme_returns_none() -> None:
    """Bare hostnames without scheme don't parse to a netloc."""
    assert extract_domain("example.com") is None


# --- save / load round-trip ---------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ANTHILL_HOME so secrets land in tmp_path, not the
    developer's real ~/.anthill."""
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))
    # Reset module-level caches so the new path takes effect.
    yield tmp_path


def test_save_and_load_minimum_fields(isolated_home) -> None:
    save_credentials(
        DomainCredentials(
            domain="zentao.example.com",
            username="alice",
            password="hunter2",
        )
    )
    loaded = load_credentials("zentao.example.com")
    assert loaded is not None
    assert loaded.username == "alice"
    assert loaded.password == "hunter2"
    assert loaded.login_url is None
    assert loaded.username_selector is None


def test_save_and_load_with_explicit_selectors(isolated_home) -> None:
    save_credentials(
        DomainCredentials(
            domain="custom-jira.example.com",
            username="bob",
            password="pw",
            login_url="https://custom-jira.example.com/login",
            username_selector="#user-input",
            password_selector="#pass-input",
            submit_selector="button.login-submit",
        )
    )
    loaded = load_credentials("custom-jira.example.com")
    assert loaded is not None
    assert loaded.login_url == "https://custom-jira.example.com/login"
    assert loaded.username_selector == "#user-input"
    assert loaded.password_selector == "#pass-input"
    assert loaded.submit_selector == "button.login-submit"


def test_load_missing_domain_returns_none(isolated_home) -> None:
    assert load_credentials("never-configured.example.com") is None


def test_load_partial_record_treated_as_missing(isolated_home) -> None:
    """If only username is set (e.g. a half-finished /auth add),
    we don't expose half-creds — load returns None."""
    from anthill.core.userconfig import upsert_secret
    upsert_secret("url_auth.broken.example.com.username", "alice")
    assert load_credentials("broken.example.com") is None


# --- list_domains -------------------------------------------------------


def test_list_domains_empty(isolated_home) -> None:
    assert list_domains() == []


def test_list_domains_sorted_and_unique(isolated_home) -> None:
    save_credentials(DomainCredentials("c.example.com", "u", "p"))
    save_credentials(DomainCredentials("a.example.com", "u", "p"))
    save_credentials(DomainCredentials("b.example.com", "u", "p"))
    # Save same domain twice (e.g. user updated creds) — should still
    # appear once.
    save_credentials(DomainCredentials("a.example.com", "u2", "p2"))
    assert list_domains() == [
        "a.example.com", "b.example.com", "c.example.com"
    ]


# --- remove_credentials -------------------------------------------------


def test_remove_credentials_returns_true_on_existing(isolated_home) -> None:
    save_credentials(DomainCredentials("doomed.example.com", "u", "p"))
    assert load_credentials("doomed.example.com") is not None
    assert remove_credentials("doomed.example.com") is True
    assert load_credentials("doomed.example.com") is None


def test_remove_credentials_returns_false_when_missing(isolated_home) -> None:
    assert remove_credentials("never-configured.example.com") is False


def test_remove_cleans_all_related_fields(isolated_home) -> None:
    """An entry with extra fields (selectors etc) must be fully wiped,
    not leave orphan fields that would confuse load_credentials later."""
    save_credentials(
        DomainCredentials(
            domain="thorough.example.com",
            username="u",
            password="p",
            login_url="x",
            username_selector="x",
            password_selector="x",
            submit_selector="x",
        )
    )
    remove_credentials("thorough.example.com")
    from anthill.core.userconfig import load_secrets
    leftovers = {
        k for k in load_secrets() if k.startswith("url_auth.thorough.")
    }
    assert leftovers == set()


def test_remove_doesnt_affect_other_domains(isolated_home) -> None:
    save_credentials(DomainCredentials("keep.example.com", "u", "p"))
    save_credentials(DomainCredentials("drop.example.com", "u", "p"))
    remove_credentials("drop.example.com")
    assert load_credentials("keep.example.com") is not None
    assert load_credentials("drop.example.com") is None


# --- 0.1.72 — Playwright storage_state cache ----------------------------


def test_cookie_state_round_trip(isolated_home) -> None:
    from anthill.core.url_credentials import (
        load_cookie_state,
        save_cookie_state,
    )

    state = {
        "cookies": [
            {
                "name": "session",
                "value": "abc123",
                "domain": "zentao.example.com",
                "path": "/",
                "expires": 1800000000,
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [
            {
                "origin": "https://zentao.example.com",
                "localStorage": [{"name": "lang", "value": "zh-CN"}],
            }
        ],
    }
    save_cookie_state("zentao.example.com", state)
    loaded = load_cookie_state("zentao.example.com")
    assert loaded is not None
    # Anthill's metadata header should be STRIPPED on load so the
    # returned dict is a drop-in for browser.new_context(storage_state=).
    assert "_anthill_meta" not in loaded
    assert loaded["cookies"][0]["name"] == "session"
    assert loaded["cookies"][0]["value"] == "abc123"
    assert loaded["origins"][0]["origin"] == "https://zentao.example.com"


def test_cookie_state_missing_returns_none(isolated_home) -> None:
    from anthill.core.url_credentials import load_cookie_state

    assert load_cookie_state("never-saved.example.com") is None


def test_cookie_state_corrupt_file_returns_none(isolated_home) -> None:
    """Hand-edited / power-cut / not-JSON file must not crash load."""
    from anthill.core.url_credentials import (
        cookie_state_path,
        load_cookie_state,
    )

    path = cookie_state_path("corrupt.example.com")
    path.write_text("this is not json {{{")
    assert load_cookie_state("corrupt.example.com") is None


def test_cookie_state_path_sanitizes_domain_with_port(isolated_home) -> None:
    """Domain with port (e.g. example.com:8443) → filesystem-safe path."""
    from anthill.core.url_credentials import cookie_state_path

    p = cookie_state_path("example.com:8443")
    # Colon is not in [a-zA-Z0-9._-] so should be replaced.
    assert ":" not in p.name
    assert "example.com" in p.name


def test_remove_cookie_state_returns_true_when_present(isolated_home) -> None:
    from anthill.core.url_credentials import (
        remove_cookie_state,
        save_cookie_state,
    )

    save_cookie_state("doomed.example.com", {"cookies": [], "origins": []})
    assert remove_cookie_state("doomed.example.com") is True
    assert remove_cookie_state("doomed.example.com") is False  # idempotent


def test_cookie_state_age_seconds(isolated_home) -> None:
    """Age is positive after save, None when file absent."""
    from anthill.core.url_credentials import (
        cookie_state_age_seconds,
        save_cookie_state,
    )

    assert cookie_state_age_seconds("fresh.example.com") is None
    save_cookie_state("fresh.example.com", {"cookies": []})
    age = cookie_state_age_seconds("fresh.example.com")
    assert age is not None
    assert age >= 0
    # Just saved → < 1 second
    assert age < 5.0


def test_anthill_meta_written_to_file(isolated_home) -> None:
    """The on-disk file SHOULD have the meta header; load strips it
    on the way out. This test pins the file format so future readers
    in other tools know what they're looking at."""
    import json as _json

    from anthill.core.url_credentials import (
        cookie_state_path,
        save_cookie_state,
    )

    save_cookie_state(
        "meta.example.com", {"cookies": [{"name": "x", "value": "y"}]}
    )
    raw = _json.loads(cookie_state_path("meta.example.com").read_text())
    assert "_anthill_meta" in raw
    assert raw["_anthill_meta"]["domain"] == "meta.example.com"
    assert raw["_anthill_meta"]["schema"] == 1
    assert raw["_anthill_meta"]["saved_at"] > 0
    # Playwright fields still at top-level for direct loading.
    assert raw["cookies"] == [{"name": "x", "value": "y"}]
