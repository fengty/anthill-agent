"""Channel abstract interface.

0.1.57 — upgraded from a flat ChannelMessage to PlatformMessage with
thread / reply / media metadata so platform-native UX can be preserved
across the channels matrix:

  - thread_id   topic-style sub-conversations (Slack thread_ts,
                Telegram message_thread_id, Lark thread.thread_id,
                Discord thread_id)
  - reply_to_id the user's message we're replying TO (for "quoted
                reply" UX on Telegram / Lark / Discord)
  - source      the channel name; lets the daemon route responses
                back without consulting external state
  - media       attachments (image / audio / file) — voice memo
                transcription lands here

ChannelMessage is kept as an alias for backward compatibility — any
caller that didn't read the new fields continues to work, and any
caller that wants thread-awareness can opt in incrementally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


MediaKind = Literal["image", "audio", "video", "file"]


@dataclass
class MediaAttachment:
    """One piece of media attached to a message.

    `kind` says what to interpret. `data` is platform-specific (URL,
    file path, or base64 — we don't unify) so the channel that produced
    the message is also the channel that knows how to fetch it.
    `mime` and `size_bytes` are best-effort hints.
    """

    kind: MediaKind
    data: str
    mime: str | None = None
    size_bytes: int | None = None
    # 0.1.57 — when the channel ran transcription / OCR before
    # forwarding, the extracted text lives here. None = not extracted.
    # Voice memos in particular put their transcript here so Scout
    # sees searchable text instead of a base64 blob.
    transcript: str | None = None


@dataclass
class PlatformMessage:
    """One message received from an IM channel.

    The mandatory fields (channel/sender/text) are unchanged from the
    old `ChannelMessage`. New optional fields capture the
    platform-native routing metadata Hermes calls out as a strength:
    thread/topic ID, reply target, media attachments.

    Channels that don't have a concept for some field just leave it
    None — the daemon side handles None gracefully.
    """

    channel: str
    sender: str
    text: str
    raw: dict | None = None
    message_id: str | None = None  # for idempotency / receipts
    # ─── 0.1.57 additions ──────────────────────────────────────────
    # Topic-style sub-conversation within the same chat. Slack uses
    # thread_ts; Telegram uses message_thread_id (groups with topics);
    # Lark uses thread.thread_id (group threads); Discord uses
    # threads as native channels. None = main channel / no thread.
    thread_id: str | None = None
    # The message THIS message is replying to (quote-reply UX).
    # Lark message.parent_id; Telegram reply_to_message.message_id;
    # Discord referenced_message.id. None = not a reply.
    reply_to_id: str | None = None
    # Convenience copy of `channel` so callers that hold a single
    # PlatformMessage don't need to also remember which channel it
    # came from. Mostly equal to `channel` — set explicitly for
    # routes that go through multiple channels (e.g. an MCP proxy).
    source: str = ""
    # Media attachments. Empty list when text-only.
    media: list[MediaAttachment] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Default source to channel if caller didn't set it.
        if not self.source:
            self.source = self.channel


# Back-compat alias — any code that imports `ChannelMessage` keeps
# working. New code should use PlatformMessage directly.
ChannelMessage = PlatformMessage


class Channel(ABC):
    """A bridge between an IM platform and the nation."""

    name: str  # short identifier ("lark", "slack", ...)

    @abstractmethod
    async def send(
        self,
        *,
        to: str,
        text: str,
        reply_to: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        """Send a message to the platform.

        thread_id (0.1.57) lets callers reply IN a specific topic /
        thread when the platform supports it. Channels that don't
        support threads just ignore it. reply_to (existing) is for
        quote-reply to a specific message_id; thread_id is for the
        ambient topic. Both can be set together (reply in thread).
        """

    @abstractmethod
    async def ping(self) -> bool:
        """Health check — returns True if credentials and connectivity look good."""
