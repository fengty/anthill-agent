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

from dataclasses import dataclass
from urllib.parse import urlparse

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
