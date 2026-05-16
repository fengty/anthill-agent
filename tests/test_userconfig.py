"""Tests for the user config + secrets schema."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from anthill.core.userconfig import (
    ChannelEntry,
    ModelEntry,
    UserConfig,
    load_config,
    load_secrets,
    mask,
    remove_secret,
    save_config,
    save_secrets,
    secret_for,
    upsert_secret,
)


@pytest.fixture(autouse=True)
def _isolate_anthill_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHILL_HOME", str(tmp_path))


def test_load_config_missing_file_returns_empty() -> None:
    cfg = load_config()
    assert cfg.models == []
    assert cfg.channels == []
    assert cfg.default_model is None


def test_save_then_load_roundtrip() -> None:
    cfg = UserConfig(
        default_model="work",
        models=[
            ModelEntry(
                name="work",
                provider="deepseek",
                model="deepseek-chat",
                secret_ref="deepseek_default",
            ),
            ModelEntry(
                name="play",
                provider="custom",
                model="my-llm",
                secret_ref="play_secret",
                base_url="https://llm.example.com/v1",
            ),
        ],
        channels=[
            ChannelEntry(
                name="work-bot",
                kind="lark",
                secret_ref="lark_work",
                extra={"app_id": "cli_abc"},
            ),
        ],
    )
    save_config(cfg)

    loaded = load_config()
    assert loaded.default_model == "work"
    assert len(loaded.models) == 2
    assert loaded.models[1].base_url == "https://llm.example.com/v1"
    assert loaded.channels[0].extra["app_id"] == "cli_abc"


def test_save_config_writes_real_file() -> None:
    cfg = UserConfig(default_model="x")
    path = save_config(cfg)
    assert path.exists()
    text = path.read_text()
    assert 'default_model = "x"' in text


def test_secrets_roundtrip() -> None:
    save_secrets({"deepseek_default": "sk-12345", "lark_work": "secret-abc"})
    secrets = load_secrets()
    assert secrets["deepseek_default"] == "sk-12345"
    assert secrets["lark_work"] == "secret-abc"


def test_secrets_file_is_chmod_600() -> None:
    save_secrets({"k": "v"})
    from anthill.core.userconfig import secrets_path
    mode = secrets_path().stat().st_mode & 0o777
    if os.name == "posix":
        assert mode == 0o600


def test_upsert_secret_preserves_others() -> None:
    save_secrets({"a": "1", "b": "2"})
    upsert_secret("c", "3")
    secrets = load_secrets()
    assert secrets == {"a": "1", "b": "2", "c": "3"}


def test_upsert_secret_overwrites_existing() -> None:
    save_secrets({"a": "1"})
    upsert_secret("a", "2")
    assert load_secrets()["a"] == "2"


def test_secret_for_returns_none_when_missing() -> None:
    assert secret_for("nope") is None


def test_remove_secret_returns_true_when_existed() -> None:
    save_secrets({"a": "1"})
    assert remove_secret("a") is True
    assert load_secrets() == {}


def test_remove_secret_returns_false_when_missing() -> None:
    assert remove_secret("nope") is False


def test_find_model_returns_entry() -> None:
    cfg = UserConfig(models=[ModelEntry("a", "deepseek", "x", "ref-a")])
    assert cfg.find_model("a").provider == "deepseek"
    assert cfg.find_model("missing") is None


def test_find_channel_returns_entry() -> None:
    cfg = UserConfig(channels=[ChannelEntry("a", "lark", "ref-a")])
    assert cfg.find_channel("a").kind == "lark"
    assert cfg.find_channel("missing") is None


def test_mask_short_secret_all_stars() -> None:
    assert mask("abcd") == "****"


def test_mask_long_secret_shows_prefix_and_suffix() -> None:
    out = mask("sk-1234567890ab")
    assert out.startswith("sk-1")
    assert out.endswith("ab")
    assert "…" in out


def test_extra_fields_survive_roundtrip() -> None:
    cfg = UserConfig(
        models=[
            ModelEntry(
                name="weird",
                provider="custom",
                model="x",
                secret_ref="r",
                extra={"timeout_seconds": 60},
            )
        ]
    )
    save_config(cfg)
    loaded = load_config()
    assert loaded.models[0].extra["timeout_seconds"] == 60
