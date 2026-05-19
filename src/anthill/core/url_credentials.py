"""0.1.71 — per-domain login credentials for the browser fallback.

When `_try_browser_fallback` hits a login wall, instead of giving up
we ask: "does the king have keys for this domain?" If yes, we let
Playwright run the login flow before fetching.

This is the "citizens serve the king" principle applied to network
auth: the agent shouldn't shrug at a corporate Zentao / Confluence /
internal Jira when the user has perfectly good credentials. It just
needs to ask once, then remember.

Storage: piggybacks on ~/.anthill/secrets.toml (which already enforces
chmod 600 + the secret-load path). Keys live under the `url_auth.*`
namespace:

  url_auth.ss.chandao.pamirs.top.username
  url_auth.ss.chandao.pamirs.top.password
  url_auth.ss.chandao.pamirs.top.login_url           (optional)
  url_auth.ss.chandao.pamirs.top.username_selector   (optional, CSS)
  url_auth.ss.chandao.pamirs.top.password_selector   (optional, CSS)
  url_auth.ss.chandao.pamirs.top.submit_selector     (optional, CSS)

Future: encrypted-at-rest. For MVP the existing 0600 + dotfile pattern
is the same protection the model API keys get.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from anthill.config import AnthillConfig
from anthill.core.userconfig import load_secrets, remove_secret, upsert_secret


# Secret-namespace prefix. Keep it stable — once written to a user's
# secrets.toml, breaking this prefix would orphan their existing
# stored credentials.
_NS = "url_auth"


@dataclass(frozen=True)
class DomainCredentials:
    """One domain's login config + creds."""

    domain: str
    username: str
    password: str
    # Login form URL. When None, we attempt to detect by fetching
    # the target URL and looking for a redirect / form on the page.
    # For Zentao, this is usually http://<host>/zentao/user-login.html
    login_url: str | None = None
    # CSS selectors for the form fields. When None, Playwright tries
    # common patterns: input[name=account|username|email] and
    # input[type=password] and the first submit/button-type element.
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None


def extract_domain(url: str) -> str | None:
    """Pull the netloc (host:port) from a URL. None on parse failure."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    return parsed.netloc or None


def load_credentials(domain: str) -> DomainCredentials | None:
    """Return creds for domain, or None when not configured."""
    secrets = load_secrets()
    prefix = f"{_NS}.{domain}."
    relevant = {
        k[len(prefix):]: v
        for k, v in secrets.items()
        if k.startswith(prefix)
    }
    if not relevant or "username" not in relevant or "password" not in relevant:
        return None
    return DomainCredentials(
        domain=domain,
        username=relevant["username"],
        password=relevant["password"],
        login_url=relevant.get("login_url"),
        username_selector=relevant.get("username_selector"),
        password_selector=relevant.get("password_selector"),
        submit_selector=relevant.get("submit_selector"),
    )


def save_credentials(creds: DomainCredentials) -> None:
    """Persist creds; subsequent calls to load_credentials(domain) read them back."""
    prefix = f"{_NS}.{creds.domain}."
    upsert_secret(f"{prefix}username", creds.username)
    upsert_secret(f"{prefix}password", creds.password)
    if creds.login_url:
        upsert_secret(f"{prefix}login_url", creds.login_url)
    if creds.username_selector:
        upsert_secret(f"{prefix}username_selector", creds.username_selector)
    if creds.password_selector:
        upsert_secret(f"{prefix}password_selector", creds.password_selector)
    if creds.submit_selector:
        upsert_secret(f"{prefix}submit_selector", creds.submit_selector)


def list_domains() -> list[str]:
    """All domains that have credentials configured."""
    secrets = load_secrets()
    prefix = f"{_NS}."
    domains: set[str] = set()
    for k in secrets:
        if not k.startswith(prefix):
            continue
        # k = "url_auth.example.com.username" → "example.com"
        rest = k[len(prefix):]
        # Strip the last segment (field name).
        domain, _, _ = rest.rpartition(".")
        if domain:
            domains.add(domain)
    return sorted(domains)


def remove_credentials(domain: str) -> bool:
    """Remove all stored fields for `domain`. Returns True iff anything was found."""
    secrets = load_secrets()
    prefix = f"{_NS}.{domain}."
    keys = [k for k in secrets if k.startswith(prefix)]
    if not keys:
        return False
    for k in keys:
        remove_secret(k)
    return True


# ---------------------------------------------------------------------------
# 0.1.72 — Playwright storage_state cache (cookie persistence)
# ---------------------------------------------------------------------------
#
# After a successful login flow, Playwright's `context.storage_state()`
# returns a JSON-serializable dict with all cookies + per-origin
# localStorage. Persisting this to disk and loading it into the NEXT
# context skips the login dance entirely (login is multi-second, cookie
# load is milliseconds).
#
# Storage layout: ~/.anthill/url_auth_state/<sanitized_domain>.json
# Sanitization: replace any non-[a-zA-Z0-9._-] char with '_' so port
# colons and other oddities can't break filesystem assumptions.


def _state_dir() -> Path:
    home = AnthillConfig.load().home
    d = home / "url_auth_state"
    d.mkdir(parents=True, exist_ok=True)
    # 0700 — only owner can list / read. Mirrors the secrets.toml
    # 0600 protection one level up.
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]")


def cookie_state_path(domain: str) -> Path:
    """Filesystem path where this domain's cached storage_state lives."""
    safe = _SANITIZE_RE.sub("_", domain)
    return _state_dir() / f"{safe}.json"


def save_cookie_state(domain: str, state: dict) -> None:
    """Persist Playwright storage_state for `domain`.

    `state` is the dict returned by `context.storage_state()` —
    typically `{"cookies": [...], "origins": [...]}`. We write 0600
    so it's owner-readable only (cookies are bearer tokens).
    """
    path = cookie_state_path(domain)
    # Add a meta header so future readers can see when this was
    # captured (Playwright's own format has no top-level metadata).
    payload = {
        "_anthill_meta": {
            "saved_at": time.time(),
            "domain": domain,
            "schema": 1,
        },
        **state,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_cookie_state(domain: str) -> dict | None:
    """Return saved storage_state for `domain`, or None when missing.

    Strips the `_anthill_meta` header so the returned dict is a
    drop-in for `browser.new_context(storage_state=...)`.
    """
    path = cookie_state_path(domain)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    # Strip our metadata; return the rest as-is.
    raw.pop("_anthill_meta", None)
    return raw


def remove_cookie_state(domain: str) -> bool:
    """Wipe the saved cookies for `domain`. True iff a file existed."""
    path = cookie_state_path(domain)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def cookie_state_age_seconds(domain: str) -> float | None:
    """Seconds since the saved storage_state was written. None when
    no file. Used by /auth status to surface staleness."""
    path = cookie_state_path(domain)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        saved_at = float(raw.get("_anthill_meta", {}).get("saved_at") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if saved_at <= 0:
        return None
    return max(0.0, time.time() - saved_at)
