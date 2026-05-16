"""Tests for Telegram and Slack channel parsers."""

from __future__ import annotations

from anthill.channels.slack import SlackChannel
from anthill.channels.telegram import TelegramChannel


# Telegram


def test_telegram_parse_text_message() -> None:
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "chat": {"id": 12345, "type": "private"},
            "text": "hello bot",
        },
    }
    msg = TelegramChannel.parse_event(payload)
    assert msg is not None
    assert msg.channel == "telegram"
    assert msg.sender == "12345"
    assert msg.text == "hello bot"
    assert msg.message_id == "42"


def test_telegram_parse_no_message() -> None:
    assert TelegramChannel.parse_event({"update_id": 1}) is None


def test_telegram_parse_no_text() -> None:
    payload = {
        "message": {
            "message_id": 1,
            "chat": {"id": 1},
            "photo": [{"file_id": "x"}],
        }
    }
    assert TelegramChannel.parse_event(payload) is None


def test_telegram_parse_empty_text() -> None:
    payload = {
        "message": {
            "message_id": 1,
            "chat": {"id": 1},
            "text": "   ",
        }
    }
    assert TelegramChannel.parse_event(payload) is None


# Slack


def test_slack_parse_text_message() -> None:
    payload = {
        "event": {
            "type": "message",
            "channel": "C0001",
            "user": "U0001",
            "text": "hello team",
            "ts": "1234567890.123",
        }
    }
    msg = SlackChannel.parse_event(payload)
    assert msg is not None
    assert msg.channel == "slack"
    assert msg.sender == "C0001"
    assert msg.text == "hello team"
    assert msg.message_id == "1234567890.123"


def test_slack_ignores_url_verification() -> None:
    assert SlackChannel.parse_event({"type": "url_verification", "challenge": "x"}) is None


def test_slack_ignores_bot_messages() -> None:
    payload = {
        "event": {
            "type": "message",
            "channel": "C0001",
            "bot_id": "B0001",
            "text": "echo",
            "ts": "1.0",
        }
    }
    assert SlackChannel.parse_event(payload) is None


def test_slack_ignores_edits() -> None:
    payload = {
        "event": {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C0001",
            "text": "edited",
            "ts": "1.0",
        }
    }
    assert SlackChannel.parse_event(payload) is None


def test_slack_ignores_non_message_events() -> None:
    payload = {"event": {"type": "channel_joined", "channel": "C0001"}}
    assert SlackChannel.parse_event(payload) is None
