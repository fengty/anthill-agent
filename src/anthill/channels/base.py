"""Channel abstract interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ChannelMessage:
    """One message received from an IM channel."""

    channel: str             # "lark", "slack", ...
    sender: str              # platform-specific identifier
    text: str                # what the user typed
    raw: dict | None = None  # original payload for advanced use
    message_id: str | None = None  # for idempotency / replies


class Channel(ABC):
    """A bridge between an IM platform and the nation."""

    name: str  # short identifier ("lark", "slack", ...)

    @abstractmethod
    async def send(self, *, to: str, text: str, reply_to: str | None = None) -> None:
        """Send a message to the platform."""

    @abstractmethod
    async def ping(self) -> bool:
        """Health check — returns True if credentials and connectivity look good."""
