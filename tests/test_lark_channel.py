"""Tests for the Lark channel — parsing and ID detection, no live calls."""

from __future__ import annotations

import json

from anthill.channels.lark import LarkChannel


def test_detect_chat_id() -> None:
    assert LarkChannel._detect_receive_id_type("oc_abc123") == "chat_id"


def test_detect_open_id() -> None:
    assert LarkChannel._detect_receive_id_type("ou_abc123") == "open_id"


def test_detect_union_id() -> None:
    assert LarkChannel._detect_receive_id_type("on_xyz") == "union_id"


def test_detect_email() -> None:
    assert LarkChannel._detect_receive_id_type("user@example.com") == "email"


def test_detect_fallback_user_id() -> None:
    assert LarkChannel._detect_receive_id_type("user-1234") == "user_id"


def test_parse_verification_returns_none() -> None:
    assert LarkChannel.parse_event({"type": "url_verification", "challenge": "x"}) is None


def test_parse_unrelated_event_returns_none() -> None:
    payload = {"header": {"event_type": "im.chat.created_v1"}, "event": {}}
    assert LarkChannel.parse_event(payload) is None


def test_parse_text_message() -> None:
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user1"}},
            "message": {
                "message_id": "om_xyz",
                "chat_id": "oc_room1",
                "message_type": "text",
                "content": json.dumps({"text": "hello nation"}),
            },
        },
    }
    msg = LarkChannel.parse_event(payload)
    assert msg is not None
    assert msg.channel == "lark"
    assert msg.text == "hello nation"
    assert msg.sender == "oc_room1"  # prefer chat for replies
    assert msg.message_id == "om_xyz"


def test_parse_text_without_chat_falls_back_to_user() -> None:
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user1"}},
            "message": {
                "message_id": "om_xyz",
                "message_type": "text",
                "content": json.dumps({"text": "direct dm"}),
            },
        },
    }
    msg = LarkChannel.parse_event(payload)
    assert msg is not None
    assert msg.sender == "ou_user1"


def test_parse_nontext_message_returns_none() -> None:
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user1"}},
            "message": {
                "message_id": "om_xyz",
                "chat_id": "oc_room1",
                "message_type": "image",
                "content": json.dumps({"image_key": "abc"}),
            },
        },
    }
    assert LarkChannel.parse_event(payload) is None


def test_parse_empty_text_returns_none() -> None:
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_user1"}},
            "message": {
                "message_id": "om_xyz",
                "chat_id": "oc_room1",
                "message_type": "text",
                "content": json.dumps({"text": "   "}),
            },
        },
    }
    assert LarkChannel.parse_event(payload) is None
