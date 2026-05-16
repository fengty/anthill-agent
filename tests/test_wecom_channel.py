"""Tests for the WeCom (企业微信) channel parser."""

from __future__ import annotations

from anthill.channels.wecom import WeComChannel


def test_parse_text_message() -> None:
    payload = {
        "MsgType": "text",
        "Content": "你好 anthill",
        "FromUserName": "ZhangSan",
        "ToUserName": "wxAgent",
        "MsgId": "1234567890",
        "AgentID": 1000002,
    }
    msg = WeComChannel.parse_event(payload)
    assert msg is not None
    assert msg.channel == "wecom"
    assert msg.text == "你好 anthill"
    assert msg.sender == "ZhangSan"
    assert msg.message_id == "1234567890"


def test_parse_lowercase_keys_accepted() -> None:
    """Some decrypt helpers lowercase field names; accept both."""
    payload = {
        "msgtype": "text",
        "content": "hi",
        "fromusername": "alice",
        "msgid": "777",
    }
    msg = WeComChannel.parse_event(payload)
    assert msg is not None
    assert msg.text == "hi"
    assert msg.sender == "alice"


def test_parse_nontext_returns_none() -> None:
    payload = {"MsgType": "image", "FromUserName": "ZhangSan", "Content": ""}
    assert WeComChannel.parse_event(payload) is None


def test_parse_empty_content_returns_none() -> None:
    payload = {"MsgType": "text", "Content": "   ", "FromUserName": "x"}
    assert WeComChannel.parse_event(payload) is None


def test_parse_missing_msgid_still_works() -> None:
    payload = {"MsgType": "text", "Content": "hello", "FromUserName": "x"}
    msg = WeComChannel.parse_event(payload)
    assert msg is not None
    assert msg.message_id is None
