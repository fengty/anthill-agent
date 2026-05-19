"""0.1.64 — voice transcription tests.

The OpenAI backend's HTTP path is stubbed; we don't actually upload
audio. What we verify:
  - NoOpBackend always returns None (safe default)
  - OpenAI backend: missing key → None (no exception)
  - OpenAI backend: success path returns text + writes attachment.transcript
  - OpenAI backend: HTTP 4xx → None
  - file path / base64 / non-existent / non-audio attachment handling
  - module-level default backend swap is idempotent
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from anthill.channels.base import MediaAttachment
from anthill.core.transcribe import (
    NoOpBackend,
    OpenAIWhisperBackend,
    get_default_backend,
    set_default_backend,
    transcribe_attachment,
    transcribe_message_audio,
)


# --- NoOpBackend ---------------------------------------------------------


@pytest.mark.asyncio
async def test_noop_backend_returns_none() -> None:
    b = NoOpBackend()
    assert await b.transcribe(b"audio", mime="audio/wav") is None


# --- OpenAI backend ------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_backend_no_key_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    b = OpenAIWhisperBackend(api_key=None)
    assert await b.transcribe(b"audio data", mime="audio/wav") is None


@pytest.mark.asyncio
async def test_openai_backend_success_returns_text(monkeypatch) -> None:
    """Successful Whisper response → transcript text."""
    import httpx

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"text": "hello world from voice memo"}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, files, headers):
            captured["url"] = url
            captured["files"] = files
            captured["auth"] = headers["Authorization"]
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    b = OpenAIWhisperBackend(api_key="sk-test")
    text = await b.transcribe(b"audio bytes", mime="audio/ogg")
    assert text == "hello world from voice memo"
    assert captured["url"].endswith("/audio/transcriptions")
    assert captured["auth"] == "Bearer sk-test"
    # File extension hint flowed from mime → .ogg.
    name, _data, _mime = captured["files"]["file"]
    assert name == "audio.ogg"


@pytest.mark.asyncio
async def test_openai_backend_http_error_returns_none(monkeypatch) -> None:
    import httpx

    class _Resp:
        status_code = 429

        def json(self):
            return {"error": "rate limit"}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    b = OpenAIWhisperBackend(api_key="sk-test")
    assert await b.transcribe(b"audio", mime="audio/wav") is None


@pytest.mark.asyncio
async def test_openai_backend_empty_text_returns_none(monkeypatch) -> None:
    """An empty/whitespace-only Whisper response shouldn't fake a hit."""
    import httpx

    class _Resp:
        status_code = 200

        def json(self):
            return {"text": "   "}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    b = OpenAIWhisperBackend(api_key="sk-test")
    assert await b.transcribe(b"audio", mime="audio/wav") is None


@pytest.mark.asyncio
async def test_openai_backend_exception_returns_none(monkeypatch) -> None:
    """Network error → None, never raises (transcription failures
    must not break message handling)."""
    import httpx

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            raise ConnectionError("network down")

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    b = OpenAIWhisperBackend(api_key="sk-test")
    assert await b.transcribe(b"x", mime="audio/wav") is None


# --- transcribe_attachment integration ------------------------------------


@pytest.mark.asyncio
async def test_transcribe_attachment_writes_transcript(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"fake audio bytes")
    att = MediaAttachment(kind="audio", data=str(audio_path), mime="audio/wav")

    class _Stub:
        name = "stub"

        async def transcribe(self, audio, *, mime=None):
            assert audio == b"fake audio bytes"
            return "stubbed transcript"

    result = await transcribe_attachment(att, backend=_Stub())
    assert result == "stubbed transcript"
    assert att.transcript == "stubbed transcript"


@pytest.mark.asyncio
async def test_transcribe_attachment_skips_non_audio() -> None:
    att = MediaAttachment(kind="image", data="/tmp/x.png")

    class _StubAlwaysSucceeds:
        name = "stub"

        async def transcribe(self, audio, *, mime=None):
            return "should-not-be-called"

    result = await transcribe_attachment(att, backend=_StubAlwaysSucceeds())
    assert result is None
    assert att.transcript is None


@pytest.mark.asyncio
async def test_transcribe_attachment_data_url(monkeypatch) -> None:
    """base64 inline data URL → decoded and passed to backend."""
    raw = b"raw audio bytes"
    b64 = base64.b64encode(raw).decode()
    data_url = f"data:audio/wav;base64,{b64}"
    att = MediaAttachment(kind="audio", data=data_url, mime="audio/wav")

    received: dict = {}

    class _Stub:
        name = "stub"

        async def transcribe(self, audio, *, mime=None):
            received["audio"] = audio
            return "ok"

    await transcribe_attachment(att, backend=_Stub())
    assert received["audio"] == raw


@pytest.mark.asyncio
async def test_transcribe_attachment_missing_file_returns_none(tmp_path) -> None:
    att = MediaAttachment(
        kind="audio", data=str(tmp_path / "does-not-exist.wav"), mime="audio/wav"
    )
    assert await transcribe_attachment(att) is None
    assert att.transcript is None


@pytest.mark.asyncio
async def test_transcribe_message_audio_walks_media(tmp_path) -> None:
    a1 = tmp_path / "a.wav"
    a1.write_bytes(b"a")
    a2 = tmp_path / "b.wav"
    a2.write_bytes(b"b")
    media = [
        MediaAttachment(kind="image", data="/tmp/i.png"),  # skipped
        MediaAttachment(kind="audio", data=str(a1), mime="audio/wav"),
        MediaAttachment(kind="audio", data=str(a2), mime="audio/wav"),
    ]

    class _Stub:
        name = "stub"

        async def transcribe(self, audio, *, mime=None):
            return f"text for {len(audio)}"

    count = await transcribe_message_audio(media, backend=_Stub())
    assert count == 2
    assert media[1].transcript == "text for 1"
    assert media[2].transcript == "text for 1"


# --- module-level default backend swap -----------------------------------


def test_default_backend_swap_is_idempotent() -> None:
    """The daemon calls set_default_backend(OpenAIWhisperBackend(...))
    on startup. Multiple calls (e.g. config reload) shouldn't keep
    stacking; the most recent one wins."""
    original = get_default_backend()
    try:
        b1 = NoOpBackend()
        b2 = OpenAIWhisperBackend(api_key="x")
        set_default_backend(b1)
        assert get_default_backend() is b1
        set_default_backend(b2)
        assert get_default_backend() is b2
    finally:
        set_default_backend(original)
