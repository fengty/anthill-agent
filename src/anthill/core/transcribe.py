"""0.1.64 — voice → text transcription for media attachments.

Hermes ships voice-memo transcription as a core feature. anthill's
0.1.57 PlatformMessage.media[].transcript field was added explicitly
for this pipeline; 0.1.64 fills the pipeline itself.

Design:

  ┌───────────────────────────┐
  │ Channel.parse_event       │
  │ (audio attachment present)│
  └────────────┬──────────────┘
               │
               ▼
  ┌───────────────────────────┐
  │ transcribe_attachment()   │  ← this module
  │ writes .transcript inline │
  └────────────┬──────────────┘
               │
               ▼
  ┌───────────────────────────┐
  │ Scout sees text, not blob │
  └───────────────────────────┘

Backends (pluggable, default = OpenAI Whisper API):
  - "openai"     POST /v1/audio/transcriptions (cheapest reliable)
  - "whisper_cpp" local binary call (offline, requires install)
  - "none"       no-op (returns input unchanged; for tests / opt-out)

We never raise on transcription failure: the channel still surfaces
the message with empty transcript and the LLM downstream gets the
"audio attachment present" signal but no extracted text. Better
silent miss than dropping the message entirely.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Protocol

from anthill.channels.base import MediaAttachment


class TranscribeBackend(Protocol):
    """One way to turn audio bytes / path into text."""

    name: str

    async def transcribe(self, audio: bytes, *, mime: str | None = None) -> str | None:
        """Return transcribed text. None on failure (caller doesn't
        rely on exception type — keeps backend errors local)."""
        ...


class NoOpBackend:
    """Default when no API key / no local whisper available.

    Returning None signals "we couldn't transcribe" so callers can
    leave the original audio attachment intact and let the LLM react
    with a generic "I see you sent audio but can't hear it" rather
    than a misleading transcript.
    """

    name = "none"

    async def transcribe(self, audio: bytes, *, mime: str | None = None) -> str | None:
        return None


class OpenAIWhisperBackend:
    """OpenAI Whisper API via /v1/audio/transcriptions.

    Cheapest reliable cloud backend. Needs OPENAI_API_KEY (or the
    explicit kwarg). Default model: whisper-1.

    Why this default over self-hosted:
      - Zero install (no whisper.cpp binary, no model weight download)
      - Works on every OS / arch anthill runs on
      - Pay-per-call ($0.006/min) — fine for occasional voice memos
      - Existing OPENAI_API_KEY in many users' env vars

    For users who want offline / cheaper: a whisper.cpp backend can
    be added in a follow-up. The Protocol shape stays stable.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "whisper-1",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def transcribe(
        self, audio: bytes, *, mime: str | None = None
    ) -> str | None:
        if not self.api_key or not audio:
            return None
        import httpx

        # OpenAI expects multipart/form-data with a file field; the
        # filename influences how they detect the codec, so we hint
        # via mime → extension lookup.
        ext = _ext_for_mime(mime)
        filename = f"audio.{ext}"
        files = {
            "file": (filename, audio, mime or "application/octet-stream"),
            "model": (None, self.model),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    files=files,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            if resp.status_code >= 400:
                return None
            data = resp.json()
            text = data.get("text")
            return text if isinstance(text, str) and text.strip() else None
        except Exception:  # noqa: BLE001 — transcription must not raise
            return None


_MIME_TO_EXT = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/m4a": "m4a",
    "audio/mp4": "m4a",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


def _ext_for_mime(mime: str | None) -> str:
    if not mime:
        return "wav"
    return _MIME_TO_EXT.get(mime.lower(), "wav")


# Module-level default backend. Overridable via `set_default_backend`
# so the daemon can swap to OpenAI on startup once the key is read,
# or tests can install NoOp.
_default_backend: TranscribeBackend = NoOpBackend()


def get_default_backend() -> TranscribeBackend:
    return _default_backend


def set_default_backend(backend: TranscribeBackend) -> None:
    """Idempotent override. Used by daemon startup."""
    global _default_backend
    _default_backend = backend


def _load_audio_bytes(data: str) -> bytes | None:
    """Resolve `attachment.data` to bytes.

    `MediaAttachment.data` can be:
      - a local file path (channels that download to disk first)
      - a URL (channels that link directly; we don't fetch here —
        that's the channel's job, since auth tokens are per-channel)
      - a base64 string (small inline media)

    For 0.1.64 MVP we support file paths + base64. URLs require the
    channel to pre-download. Returning None makes the caller skip
    transcription gracefully.
    """
    if not data:
        return None
    if data.startswith("data:"):
        # data:audio/wav;base64,XXX
        try:
            import base64
            comma = data.find(",")
            if comma < 0:
                return None
            payload = data[comma + 1 :]
            return base64.b64decode(payload)
        except Exception:  # noqa: BLE001
            return None
    p = Path(data)
    if p.exists() and p.is_file():
        try:
            return p.read_bytes()
        except OSError:
            return None
    return None


async def transcribe_attachment(
    attachment: MediaAttachment,
    *,
    backend: TranscribeBackend | None = None,
) -> str | None:
    """Transcribe one audio attachment in place.

    Returns the transcript text (also written to attachment.transcript)
    so callers can immediately use it. None when there's nothing to
    transcribe — non-audio attachment, no backend, or backend failed.
    """
    if attachment.kind != "audio":
        return None
    audio = _load_audio_bytes(attachment.data)
    if audio is None:
        return None
    chosen = backend or _default_backend
    text = await chosen.transcribe(audio, mime=attachment.mime)
    if text:
        attachment.transcript = text
    return text


async def transcribe_message_audio(
    media: Iterable[MediaAttachment],
    *,
    backend: TranscribeBackend | None = None,
) -> int:
    """Walk a message's media list, transcribe each audio attachment.

    Returns the count of attachments successfully transcribed. Used
    by the channel daemon right after parse_event so downstream code
    (Scout, episodic memory) sees text not binary.
    """
    count = 0
    for att in media:
        if att.kind != "audio":
            continue
        text = await transcribe_attachment(att, backend=backend)
        if text:
            count += 1
    return count
