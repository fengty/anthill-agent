"""0.2.40 — Visual evaluation via multimodal models.

Citizens call `visual_check(screenshot, expected)` and a multimodal
model returns MATCH or MISMATCH. Tests cover:

  - verdict parsing (MATCH / MISMATCH / unknown)
  - image base64 encoding (PNG, JPEG, GIF, unsupported)
  - prompt construction with OpenAI-compat image_url format
  - executor: missing args / missing file / no provider configured /
    successful judge-result round-trip
  - end-to-end via a fake provider returning canned text

We never hit a real vision model — the tests use a stub provider
implementing complete_with_messages.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from anthill.core.tools_protocol import ToolCall
from anthill.core.vision import (
    VISUAL_CHECK,
    VisionResult,
    build_vision_messages,
    encode_image_base64,
    judge_screenshot,
    make_visual_check_executor,
    parse_vision_verdict,
)
from anthill.models.base import ModelProvider, ModelResponse


# --- a 1x1 PNG for tests (smallest valid PNG) ------------------------

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
    "YGD4DwABBAEAUOyrEgAAAABJRU5ErkJggg=="
)


# --- verdict parsing ----------------------------------------------------


def test_parse_match() -> None:
    text = (
        "Looking at the screenshot, I see the error popup with red text.\n"
        "VISUAL_VERDICT: MATCH error popup visible and centered"
    )
    v, reason = parse_vision_verdict(text)
    assert v == "match"
    assert "popup visible" in reason


def test_parse_mismatch() -> None:
    text = "The popup is missing.\nVISUAL_VERDICT: MISMATCH no popup visible"
    v, reason = parse_vision_verdict(text)
    assert v == "mismatch"
    assert "no popup" in reason


def test_parse_missing_verdict_is_unknown() -> None:
    text = "I see some stuff but I'm not sure how to verdict."
    v, _ = parse_vision_verdict(text)
    assert v == "unknown"


def test_parse_last_verdict_wins() -> None:
    """If the model rambles with intermediate verdicts, the final one
    is canonical."""
    text = (
        "Initially I thought VISUAL_VERDICT: MATCH but on closer look\n"
        "the icon is wrong color.\n"
        "VISUAL_VERDICT: MISMATCH icon should be red but is blue"
    )
    v, _ = parse_vision_verdict(text)
    assert v == "mismatch"


def test_parse_case_insensitive() -> None:
    text = "visual_verdict: match looks correct"
    v, _ = parse_vision_verdict(text)
    assert v == "match"


# --- image encoding ----------------------------------------------------


def test_encode_png(tmp_path: Path) -> None:
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_1X1)
    mime, data = encode_image_base64(p)
    assert mime == "image/png"
    # Round-trip through base64.
    assert base64.b64decode(data) == _PNG_1X1


def test_encode_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "shot.jpg"
    p.write_bytes(b"fake-jpeg-data")
    mime, _ = encode_image_base64(p)
    assert mime == "image/jpeg"


def test_encode_rejects_unsupported_suffix(tmp_path: Path) -> None:
    p = tmp_path / "shot.bmp"
    p.write_bytes(b"x")
    with pytest.raises(ValueError, match="unsupported"):
        encode_image_base64(p)


def test_encode_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        encode_image_base64(tmp_path / "nope.png")


# --- prompt construction ---------------------------------------------


def test_build_vision_messages_shape(tmp_path: Path) -> None:
    """OpenAI-compat: one user message with mixed text + image_url
    content parts."""
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_1X1)
    msgs = build_vision_messages(p, "error popup visible center-top")
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["role"] == "user"
    assert isinstance(msg["content"], list)
    # First part: text with the expected description.
    text_part = msg["content"][0]
    assert text_part["type"] == "text"
    assert "error popup visible" in text_part["text"]
    # Second part: image_url with base64 data URL.
    img_part = msg["content"][1]
    assert img_part["type"] == "image_url"
    assert img_part["image_url"]["url"].startswith("data:image/png;base64,")


# --- executor: defensive paths ---------------------------------------


def _missing_provider_executor():
    return make_visual_check_executor(vision_provider=None)


def test_executor_no_provider_returns_setup_hint() -> None:
    """Without a vision model, the tool returns a clear setup
    instruction rather than crashing."""
    exec_fn = _missing_provider_executor()
    result = asyncio.run(exec_fn(ToolCall(
        id="t1", name="visual_check",
        arguments={"screenshot": "/tmp/x.png", "expected": "anything"},
    )))
    assert result.is_error
    assert "vision model" in result.content
    assert "vision_model" in result.content  # the values key


def test_executor_missing_screenshot_arg() -> None:
    exec_fn = _missing_provider_executor()
    result = asyncio.run(exec_fn(ToolCall(
        id="t1", name="visual_check",
        arguments={"expected": "x"},
    )))
    assert result.is_error
    assert "screenshot" in result.content


def test_executor_missing_expected_arg() -> None:
    exec_fn = _missing_provider_executor()
    result = asyncio.run(exec_fn(ToolCall(
        id="t1", name="visual_check",
        arguments={"screenshot": "/tmp/x.png"},
    )))
    assert result.is_error
    assert "expected" in result.content


# --- executor: real judge round-trip via fake provider ---------------


class _FakeVisionProvider(ModelProvider):
    """Returns a canned model output. Tests inject this so we don't
    need a real multimodal API."""

    name = "fake-vision"

    def __init__(self, canned: str):
        self.canned = canned
        self.last_messages = None

    async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
        return ModelResponse(text="(fallback)", model="fake-vision")

    async def complete_with_messages(
        self, messages, *, system=None, tools=None,
        max_tokens=4096, temperature=0.7,
    ):
        self.last_messages = messages
        return ModelResponse(
            text=self.canned,
            model="fake-vision",
            input_tokens=100,
            output_tokens=20,
        )


def test_judge_screenshot_match(tmp_path: Path) -> None:
    """Full path: build messages, call provider, parse verdict."""
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_1X1)
    provider = _FakeVisionProvider(
        canned="I see the popup.\nVISUAL_VERDICT: MATCH popup visible top-center"
    )
    result = asyncio.run(judge_screenshot(
        p, "error popup visible", provider=provider,
        model_name="claude-3-5-sonnet",
    ))
    assert isinstance(result, VisionResult)
    assert result.verdict == "match"
    assert "popup visible" in result.reason
    assert result.model == "claude-3-5-sonnet"
    # The provider got an image-containing message.
    assert provider.last_messages is not None
    content = provider.last_messages[0]["content"]
    assert any(p["type"] == "image_url" for p in content)


def test_executor_e2e_match(tmp_path: Path) -> None:
    """Executor wires file → provider → verdict → ToolResult."""
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_1X1)
    provider = _FakeVisionProvider(
        canned="Looks right.\nVISUAL_VERDICT: MATCH everything in place"
    )
    exec_fn = make_visual_check_executor(
        vision_provider=provider, vision_model_name="claude-3-5-sonnet",
    )
    result = asyncio.run(exec_fn(ToolCall(
        id="t1", name="visual_check",
        arguments={"screenshot": str(p), "expected": "everything in place"},
    )))
    assert not result.is_error
    assert "VISUAL_VERDICT: MATCH" in result.content
    assert "claude-3-5-sonnet" in result.content


def test_executor_e2e_mismatch_flags_error(tmp_path: Path) -> None:
    """When the verdict is MISMATCH, the tool result has is_error=True
    so the citizen's agent loop sees the failure signal."""
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_1X1)
    provider = _FakeVisionProvider(
        canned="wrong layout.\nVISUAL_VERDICT: MISMATCH header misplaced"
    )
    exec_fn = make_visual_check_executor(vision_provider=provider)
    result = asyncio.run(exec_fn(ToolCall(
        id="t1", name="visual_check",
        arguments={"screenshot": str(p), "expected": "header centered"},
    )))
    assert result.is_error
    assert "MISMATCH" in result.content


def test_executor_handles_missing_file(tmp_path: Path) -> None:
    provider = _FakeVisionProvider(canned="...")
    exec_fn = make_visual_check_executor(vision_provider=provider)
    result = asyncio.run(exec_fn(ToolCall(
        id="t1", name="visual_check",
        arguments={"screenshot": str(tmp_path / "ghost.png"), "expected": "x"},
    )))
    assert result.is_error
    assert "not found" in result.content


# --- tool spec serializes -------------------------------------------


def test_visual_check_spec_has_required_schema() -> None:
    """The schema declares screenshot + expected as required."""
    openai = VISUAL_CHECK.to_openai_format()
    params = openai["function"]["parameters"]
    assert set(params["required"]) == {"screenshot", "expected"}
    assert "screenshot" in params["properties"]
    assert "expected" in params["properties"]


def test_visual_check_in_builtin_tools_when_opted_in() -> None:
    """builtin_tools(include_vision=True) exposes visual_check;
    default doesn't."""
    from anthill.core.tools_protocol import builtin_tools
    without = [t.name for t in builtin_tools()]
    assert "visual_check" not in without
    with_v = [t.name for t in builtin_tools(include_vision=True)]
    assert "visual_check" in with_v
