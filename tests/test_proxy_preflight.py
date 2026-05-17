"""0.1.20 — proxy preflight + SOCKS-error classification.

A real user hit a frustrating bug: ALL_PROXY=socks5://... was set
(shadowsocks / clash setup), httpx auto-followed it, but couldn't
without `socksio`. Every ask failed with "(unknown)" three times.

This patch:
1. Adds `httpx[socks]` to dependencies so fresh installs work.
2. Classifies SOCKS / proxy errors as FailureReason.NETWORK so the
   retry log says "(network)" instead of "(unknown)".
3. Adds a preflight check that runs once at REPL startup and warns
   when a SOCKS proxy is set but socksio isn't installed.
"""

from __future__ import annotations

import sys


def test_socks_error_classifies_as_network() -> None:
    """The exact string the user saw should classify as NETWORK."""
    from anthill.core.failure import FailureReason, classify_attempt

    msg = "Using SOCKS proxy, but the 'socksio' package is not installed."
    assert classify_attempt(msg) == FailureReason.NETWORK


def test_general_proxy_error_classifies_as_network() -> None:
    """Other proxy error strings also bucket to NETWORK."""
    from anthill.core.failure import FailureReason, classify_attempt

    assert classify_attempt("proxy connection refused") == FailureReason.NETWORK


def test_proxy_preflight_silent_when_no_proxy(monkeypatch, capsys) -> None:
    """No ALL_PROXY → no output at all."""
    from anthill.cli.repl import _proxy_preflight

    for var in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    _proxy_preflight()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_proxy_preflight_silent_for_http_proxy(monkeypatch, capsys) -> None:
    """An http:// or https:// proxy works without socksio — no warning."""
    from anthill.cli.repl import _proxy_preflight

    monkeypatch.setenv("ALL_PROXY", "http://corp-proxy.example.com:8080")
    _proxy_preflight()
    captured = capsys.readouterr()
    assert "socksio" not in captured.out


def test_proxy_preflight_warns_on_socks_without_socksio(monkeypatch, capsys) -> None:
    """SOCKS proxy + missing socksio → friendly warning before first ask."""
    from anthill.cli import repl as repl_mod

    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")

    # Force ImportError when the preflight tries `import socksio`.
    original_socksio = sys.modules.pop("socksio", None)
    monkeypatch.setitem(sys.modules, "socksio", None)
    try:
        repl_mod._proxy_preflight()
    finally:
        if original_socksio is not None:
            sys.modules["socksio"] = original_socksio
        else:
            sys.modules.pop("socksio", None)

    captured = capsys.readouterr()
    assert "SOCKS proxy" in captured.out
    assert "socksio" in captured.out
    # Both remedies surfaced.
    assert "pip install" in captured.out
    assert "unset ALL_PROXY" in captured.out


def test_proxy_preflight_silent_when_socksio_present(monkeypatch, capsys) -> None:
    """SOCKS proxy set AND socksio importable → no warning needed."""
    from anthill.cli import repl as repl_mod

    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    # Inject a fake socksio module so the import succeeds.
    fake = type(sys)("socksio")
    monkeypatch.setitem(sys.modules, "socksio", fake)
    repl_mod._proxy_preflight()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_pyproject_pins_httpx_socks() -> None:
    """Guard the dependency line so a future cleanup doesn't drop it."""
    import re
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text()
    assert re.search(r'"httpx\[socks\]', text), (
        "httpx[socks] dependency missing — SOCKS-proxy users will hit "
        "the 0.1.19 'socksio not installed' bug again."
    )
