"""Tests for `anthill channel` subcommands."""

from __future__ import annotations


from click.testing import CliRunner

from anthill.cli.channel_cmd import build_channel, channel as channel_group
from anthill.core.userconfig import load_config, load_secrets


def test_list_empty_shows_hint() -> None:
    result = CliRunner().invoke(channel_group, ["list"])
    assert result.exit_code == 0
    assert "No channels yet" in result.output


def test_add_lark_non_interactive() -> None:
    result = CliRunner().invoke(
        channel_group,
        [
            "add", "work-bot",
            "--kind", "lark",
            "--app-id", "cli_abc",
            "--app-secret", "secret-xyz",
        ],
    )
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert cfg.find_channel("work-bot") is not None
    entry = cfg.find_channel("work-bot")
    assert entry.kind == "lark"
    assert entry.extra["app_id"] == "cli_abc"
    secrets = load_secrets()
    assert "channel.work-bot.app_secret" in secrets
    assert secrets["channel.work-bot.app_secret"] == "secret-xyz"


def test_add_telegram_non_interactive() -> None:
    result = CliRunner().invoke(
        channel_group,
        ["add", "tg", "--kind", "telegram", "--bot-token", "123:abc"],
    )
    assert result.exit_code == 0
    secrets = load_secrets()
    assert secrets["channel.tg.bot_token"] == "123:abc"


def test_add_wecom_requires_three_fields() -> None:
    # Missing agent-id should fail.
    result = CliRunner().invoke(
        channel_group,
        [
            "add", "company",
            "--kind", "wecom",
            "--corp-id", "ww",
            "--corp-secret", "s",
        ],
    )
    assert result.exit_code != 0
    assert "Missing" in result.output


def test_add_refuses_existing_name() -> None:
    runner = CliRunner()
    runner.invoke(channel_group, ["add", "x", "--kind", "telegram", "--bot-token", "t"])
    result = runner.invoke(
        channel_group,
        ["add", "x", "--kind", "telegram", "--bot-token", "u"],
    )
    assert result.exit_code != 0


def test_show_masks_secrets() -> None:
    runner = CliRunner()
    runner.invoke(channel_group, ["add", "tg", "--kind", "telegram", "--bot-token", "12345abcdef"])
    result = runner.invoke(channel_group, ["show", "tg"])
    assert result.exit_code == 0
    assert "12345abcdef" not in result.output  # full token must not appear
    assert "1234" in result.output  # prefix shows up


def test_remove_with_yes_drops_channel_and_secrets() -> None:
    runner = CliRunner()
    runner.invoke(channel_group, ["add", "tg", "--kind", "telegram", "--bot-token", "abc"])
    result = runner.invoke(channel_group, ["remove", "tg", "--yes"])
    assert result.exit_code == 0
    cfg = load_config()
    assert cfg.find_channel("tg") is None
    assert "channel.tg.bot_token" not in load_secrets()


def test_build_channel_returns_none_for_missing_secrets() -> None:
    from anthill.core.userconfig import ChannelEntry, UserConfig, save_config
    save_config(
        UserConfig(
            channels=[
                ChannelEntry(name="x", kind="telegram", secret_ref="channel.x", extra={})
            ]
        )
    )
    entry = load_config().find_channel("x")
    assert build_channel(entry) is None


def test_build_channel_constructs_telegram_when_complete() -> None:
    runner = CliRunner()
    runner.invoke(channel_group, ["add", "t", "--kind", "telegram", "--bot-token", "abc"])
    entry = load_config().find_channel("t")
    ch = build_channel(entry)
    assert ch is not None
    assert ch.name == "telegram"
