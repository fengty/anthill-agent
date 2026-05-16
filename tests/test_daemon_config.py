"""Tests for the daemon config parser (no FastAPI deps required)."""

from __future__ import annotations

import pytest

from anthill.channels.daemon import DaemonConfig


def test_default_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in [
        "ANTHILL_DAEMON_NATION",
        "ANTHILL_DAEMON_HOST",
        "ANTHILL_DAEMON_PORT",
        "ANTHILL_LARK_APP_ID",
        "ANTHILL_LARK_APP_SECRET",
        "ANTHILL_LARK_VERIFICATION_TOKEN",
    ]:
        monkeypatch.delenv(var, raising=False)
    cfg = DaemonConfig.from_env()
    assert cfg.nation_name == "default"
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8765
    assert cfg.lark_app_id is None


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_DAEMON_NATION", "kingdom")
    monkeypatch.setenv("ANTHILL_DAEMON_HOST", "127.0.0.1")
    monkeypatch.setenv("ANTHILL_DAEMON_PORT", "9000")
    monkeypatch.setenv("ANTHILL_LARK_APP_ID", "cli_abc")
    monkeypatch.setenv("ANTHILL_LARK_APP_SECRET", "secret123")
    cfg = DaemonConfig.from_env()
    assert cfg.nation_name == "kingdom"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 9000
    assert cfg.lark_app_id == "cli_abc"
    assert cfg.lark_app_secret == "secret123"


def test_build_app_requires_fastapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """If FastAPI is missing, build_app should error clearly. We can't
    really uninstall FastAPI in test, so this is a tautological check:
    when FastAPI IS available, build_app returns something app-shaped."""
    from anthill.channels.daemon import build_app

    cfg = DaemonConfig(nation_name="default")
    try:
        app = build_app(cfg)
    except RuntimeError as e:
        # FastAPI not installed in test env — accept gracefully.
        assert "daemon" in str(e)
        return

    # FastAPI present: just verify the routes registered.
    routes = {r.path for r in app.routes}
    assert "/health" in routes
    assert "/lark/webhook" in routes
