"""Tests for v0.2.9 secrets hardening."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from anthill.core.userconfig import (
    ChannelEntry,
    ModelEntry,
    UserConfig,
    audit_secrets_permissions,
    config_dir,
    save_config,
    save_secrets,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


def test_save_secrets_emits_gitignore() -> None:
    save_secrets({"k": "v"})
    gi = config_dir() / ".gitignore"
    assert gi.exists()
    text = gi.read_text()
    assert "secrets.toml" in text
    assert "nations/" in text
    assert "history.jsonl" in text


def test_audit_when_missing() -> None:
    result = audit_secrets_permissions()
    assert result["exists"] is False


def test_audit_fixes_drifted_mode() -> None:
    if os.name != "posix":
        pytest.skip("chmod semantics differ on this OS")
    save_secrets({"k": "v"})
    from anthill.core.userconfig import secrets_path
    path = secrets_path()
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)  # 0o644
    audit = audit_secrets_permissions()
    assert audit["mode_ok"] is True
    assert audit["fixed"] is True
    # Mode should now be back to 0o600.
    assert (path.stat().st_mode & 0o777) == 0o600


def test_save_config_rejects_inlined_secret_in_model_extra() -> None:
    cfg = UserConfig(
        models=[
            ModelEntry(
                name="bad",
                provider="custom",
                model="x",
                secret_ref="ref",
                extra={"api_key": "sk-12345678901234567890"},  # secret inlined!
            )
        ]
    )
    with pytest.raises(RuntimeError, match="looks like a secret"):
        save_config(cfg)


def test_save_config_rejects_inlined_secret_in_channel_extra() -> None:
    cfg = UserConfig(
        channels=[
            ChannelEntry(
                name="bad",
                kind="lark",
                secret_ref="ref",
                extra={"app_secret": "xoxb-leaked-token-here"},
            )
        ]
    )
    with pytest.raises(RuntimeError, match="looks like a secret"):
        save_config(cfg)


def test_save_config_allows_legitimate_string_fields() -> None:
    """Short strings, IDs, etc. must not trigger the safeguard."""
    cfg = UserConfig(
        models=[
            ModelEntry(
                name="ok",
                provider="custom",
                model="my-model",
                secret_ref="ref",
                base_url="https://example.com/v1",
            )
        ],
        channels=[
            ChannelEntry(
                name="ok-ch",
                kind="lark",
                secret_ref="channel.ok-ch",
                extra={"app_id": "cli_abc123"},
            )
        ],
    )
    # Should not raise.
    save_config(cfg)
