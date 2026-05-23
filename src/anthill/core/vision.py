"""0.2.40 — Visual evaluation: ask a multimodal model about a screenshot.

Text/selector-based verification catches a lot, but visual bugs
(misaligned layout, missing icons, popup cut off, spinner stuck)
need a HUMAN-LIKE LOOK. Citizens can now call:

  [[visual_check:screenshot=foo.png expected="error popup visible"]]

…and a multimodal model (Claude 3.5 / GPT-4o / Gemini) returns
MATCH or MISMATCH + a one-line reason.

The vision model is configured separately from the citizen models
(many citizen-class models lack vision). When unconfigured, the
tool returns a clear error telling the user how to set it up:
  $ anthill values set vision_model claude-3-5-sonnet-20241022

Scope of this version:
  - Single image per call (a screenshot)
  - JPEG / PNG only (Playwright outputs PNG; users can paste either)
  - Single multimodal judge call (no multi-turn)
  - OpenAI-compatible chat/completions image format (works with GPT-4o,
    Claude via the Anthropic OpenAI-compat endpoint, Gemini OpenAI-compat,
    most Chinese providers' vision APIs).

Out of scope (later):
  - True Anthropic Messages-API image format (separate path)
  - Multi-image diff ("did anything change between A and B?")
  - PDF / video frames
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from anthill.core.tools_protocol import ToolCall, ToolResult, ToolSpec


VISUAL_CHECK = ToolSpec(
    name="visual_check",
    description=(
        "Send a screenshot to a multimodal model and ask whether it "
        "matches a described expected state. Use AFTER taking a "
        "[[browser:screenshot ...]] when text/selector checks aren't "
        "enough — e.g., 'is the error popup actually visible?', 'is "
        "the layout correct?', 'is the spinner showing?'. Returns "
        "MATCH or MISMATCH + a one-line reason. Costs one vision-"
        "model call (~$0.005) per invocation; don't spam."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "screenshot": {
                "type": "string",
                "description": (
                    "Path to the screenshot PNG/JPEG. Usually the path "
                    "returned by a prior browser_action screenshot call."
                ),
            },
            "expected": {
                "type": "string",
                "description": (
                    "Plain-text description of what the screenshot "
                    "should show. Be specific: 'red error popup with "
                    "text \"invalid credentials\" visible center-top'."
                ),
            },
        },
        "required": ["screenshot", "expected"],
    },
)


_VISUAL_PROMPT = """\
You are a visual QA inspector. Look at the attached screenshot.

The expected state is:
{expected}

Does the screenshot MATCH that expected state?

End your response with EXACTLY one line:
  VISUAL_VERDICT: MATCH <one-line reason>
  or
  VISUAL_VERDICT: MISMATCH <one-line reason>

Be specific about what you see (or don't see). If the expected
state mentions specific text, look for that exact text. If it
mentions colors / position / visibility, check those.
"""


@dataclass
class VisionResult:
    """Outcome of one visual_check call."""

    verdict: str       # "match" | "mismatch" | "unknown"
    reason: str        # short human-readable explanation
    model: str         # which vision model judged
    raw_output: str    # full model response for debugging


_VERDICT_RE = re.compile(
    r"\bVISUAL_VERDICT\s*:\s*(?P<v>MATCH|MISMATCH)\b(?P<reason>[^\n]*)",
    re.IGNORECASE,
)


def parse_vision_verdict(text: str) -> tuple[str, str]:
    """Pull VISUAL_VERDICT: MATCH/MISMATCH out of model output.

    Returns (verdict, reason). Verdict ∈ {"match", "mismatch", "unknown"}.
    """
    if not text:
        return ("unknown", "no output")
    matches = list(_VERDICT_RE.finditer(text))
    if not matches:
        return ("unknown", "no VISUAL_VERDICT line found")
    m = matches[-1]
    v = m.group("v").lower()
    reason = (m.group("reason") or "").strip(" :-—") or v
    return (v, reason)


def encode_image_base64(path: Path) -> tuple[str, str]:
    """Read an image file and return (mime_type, base64-encoded data)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"screenshot not found: {p}")
    suffix = p.suffix.lower().lstrip(".")
    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(suffix)
    if mime is None:
        raise ValueError(
            f"unsupported image type: {suffix!r}. Use PNG/JPEG."
        )
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return (mime, data)


def build_vision_messages(image_path: Path, expected: str) -> list[dict]:
    """Construct OpenAI-compat chat-completions messages with an image.

    Format: [{role: user, content: [{type: text, text: ...},
                                    {type: image_url, image_url: {url: "data:..."}}]}]

    This format is broadly compatible: OpenAI itself, Claude via OAI-
    compat endpoint, Gemini OAI-compat, qwen-vl, glm-4v, etc. Pure
    Anthropic Messages API needs a different shape (later).
    """
    mime, b64 = encode_image_base64(image_path)
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": _VISUAL_PROMPT.format(expected=expected.strip()),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
            ],
        }
    ]


async def judge_screenshot(
    image_path: Path,
    expected: str,
    *,
    provider,  # ModelProvider with complete_with_messages
    model_name: str = "",
    max_tokens: int = 400,
) -> VisionResult:
    """Send the screenshot to a multimodal provider and return verdict.

    The provider must support image content in complete_with_messages.
    Right now that's the OpenAI-compat path with image_url content
    parts — works with GPT-4o, Claude via OpenAI-compat, Gemini OAI,
    qwen-vl, glm-4v, etc.
    """
    messages = build_vision_messages(image_path, expected)
    response = await provider.complete_with_messages(
        messages, tools=None, max_tokens=max_tokens, temperature=0.1,
    )
    verdict, reason = parse_vision_verdict(response.text or "")
    return VisionResult(
        verdict=verdict,
        reason=reason,
        model=model_name or getattr(provider, "model", "unknown"),
        raw_output=response.text or "",
    )


def make_visual_check_executor(
    *,
    vision_provider=None,  # ModelProvider or None
    vision_model_name: str = "",
):
    """Build the tool executor that dispatches visual_check calls.

    `vision_provider` is a configured ModelProvider that supports
    multimodal complete_with_messages. When None, every call returns
    a structured error telling the citizen no vision model is wired.
    """

    async def execute(call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        screenshot_path = args.get("screenshot")
        expected = args.get("expected")
        if not screenshot_path or not isinstance(screenshot_path, str):
            return ToolResult(
                tool_call_id=call.id,
                content="visual_check requires `screenshot` path string",
                is_error=True,
            )
        if not expected or not isinstance(expected, str):
            return ToolResult(
                tool_call_id=call.id,
                content="visual_check requires `expected` description string",
                is_error=True,
            )
        if vision_provider is None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "no vision model configured. Set one with:\n"
                    "  anthill values set vision_model <model>\n"
                    "(e.g. claude-3-5-sonnet / gpt-4o / qwen-vl-max)"
                ),
                is_error=True,
            )
        p = Path(screenshot_path).expanduser()
        if not p.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=f"screenshot not found: {p}",
                is_error=True,
            )
        try:
            result = await judge_screenshot(
                p, expected,
                provider=vision_provider,
                model_name=vision_model_name,
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=f"vision call failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        # Format the tool result so the citizen can react.
        is_match = result.verdict == "match"
        head = (
            f"VISUAL_VERDICT: MATCH"
            if is_match else
            f"VISUAL_VERDICT: {result.verdict.upper()}"
        )
        body = (
            f"{head}\n"
            f"reason: {result.reason}\n"
            f"model: {result.model}"
        )
        return ToolResult(
            tool_call_id=call.id,
            content=body,
            is_error=not is_match,
        )

    return execute
