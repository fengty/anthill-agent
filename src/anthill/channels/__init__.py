"""Channels — IM platforms the nation can be reached on.

Each channel is a thin adapter that receives messages, forwards them to
the nation's ask() pipeline, and posts the answer back. Hermes ships
adapters for Lark/Feishu, Slack, Telegram, WhatsApp, Discord, iMessage.
Anthill starts with Lark and grows from there.

A channel does NOT do its own intelligence — it just wires the human
side to the nation. Authentication, message framing, and idempotency
live here; reasoning lives in Nation.ask.
"""

from anthill.channels.base import Channel, ChannelMessage

__all__ = ["Channel", "ChannelMessage"]
