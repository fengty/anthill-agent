"""0.2.17 — Lightweight follow-up suggestions after each ask.

A subtle UX gap: after answering, Anthill goes silent until the
user thinks of what to ask next. Hermes inserts 1-2 "你可能还想
问" inline. We do the same — BUT cheap: no LLM call, pure rule
heuristics over the request and final output.

Rules of the game:
  - Return 0–2 short hints (≤30 chars each in Chinese, or ~50 ASCII)
  - Hints are SUGGESTIONS not commands — the user reads & ignores
  - Never duplicate what the user already asked
  - Empty list is a perfectly fine signal: "nothing obvious"

Why rule-based, not an LLM:
  - Already burned $0.001 on the actual ask; another LLM call for
    follow-ups would double that cost for 5 lines of text
  - User experience > algorithmic elegance — "你可能还想问 X" is
    valuable even when it's 70% rule-of-thumb
  - LLM follow-ups can be added later as an optional toggle
"""

from __future__ import annotations

import re

# Detect a code block (markdown-style ``` fence or ≥3 indented lines
# starting with `$`/`>`/`#` — common shell paste markers).
_CODE_FENCE = re.compile(r"```", re.MULTILINE)
_SHELL_PROMPT = re.compile(r"^[ \t]*[\$>#]\s", re.MULTILINE)
_URL = re.compile(r"https?://[^\s)]+")
_FILE_PATH = re.compile(r"(?:^|\s)([./~][\w./\-]*\.\w{1,6})(?:\s|$)")

# "Definition" verbs — when the user asked "what is X" they often
# want a follow-up like "how to use" or "compared to Y".
_DEF_PATTERNS = (
    "什么是", "是什么", "what is", "what's",
)
_HOW_PATTERNS = (
    "怎么", "如何", "how do", "how to",
)
_COMPARE_PATTERNS = (
    "vs", "对比", "区别", "差异", "compared",
)


def _has_code(text: str) -> bool:
    if _CODE_FENCE.search(text):
        return True
    # Detect ≥3 lines that look like shell commands → loose code.
    lines = _SHELL_PROMPT.findall(text)
    return len(lines) >= 3


def _request_kind(req: str) -> str:
    """Coarse classification used to pick relevant follow-ups.

    Returns one of: 'definition', 'how', 'compare', 'other'.
    """
    low = req.lower()
    if any(p in low for p in _COMPARE_PATTERNS):
        return "compare"
    if any(p in low for p in _DEF_PATTERNS):
        return "definition"
    if any(p in low for p in _HOW_PATTERNS):
        return "how"
    return "other"


def suggest_followups(request: str, final_output: str) -> list[str]:
    """Pick 0–2 follow-up hints for the user.

    Pure function — easy to test. The REPL calls this after rendering
    the final output and prints whatever non-empty list it returns.
    """
    req = (request or "").strip()
    out = (final_output or "").strip()
    if not out:
        return []

    hints: list[str] = []
    kind = _request_kind(req)

    # Code-presence implies the user might want runnability help.
    has_code = _has_code(out)

    # URLs in output → invite a deeper look.
    has_url = bool(_URL.search(out))

    # File paths mentioned → "want me to inspect them?"
    has_paths = bool(_FILE_PATH.search(out))

    # Output very long → "want it summarized?"
    is_long = len(out) > 2000

    # --- definition asks: usually want next-step or comparison ----
    if kind == "definition":
        if has_code:
            hints.append("想要一个跑得起来的最小例子吗？")
        else:
            hints.append("想看它跟同类方案的对比吗？")
        hints.append("想知道实际怎么用吗？")
    # --- how-to asks: usually want pitfalls or a worked example ---
    elif kind == "how":
        if has_code:
            hints.append("想看完整可运行的例子吗？")
        hints.append("想知道这条路常见的坑吗？")
    # --- compare asks: usually want a recommendation ------------
    elif kind == "compare":
        hints.append("想要一个针对你场景的建议吗？")
        if has_code:
            hints.append("想看两边各自的最小例子吗？")
    # --- generic asks: lean on output shape ---------------------
    else:
        if has_code and not is_long:
            hints.append("想要一步步带着跑一遍吗？")
        elif has_paths:
            hints.append("想让我直接打开这些文件看看吗？")
        elif has_url:
            hints.append("想让我打开链接看具体内容吗？")
        if is_long:
            hints.append("想要一份 100 字以内的精简版吗？")

    # De-dup + cap at 2.
    seen: set[str] = set()
    unique: list[str] = []
    for h in hints:
        if h in seen:
            continue
        seen.add(h)
        unique.append(h)
        if len(unique) >= 2:
            break

    # Drop any hint that's basically what the user just asked.
    low_req = req.lower()
    def _too_similar(hint: str) -> bool:
        # cheap token-set Jaccard, ≥0.5 means "same question".
        h = set(hint.lower().split())
        r = set(low_req.split())
        if not h or not r:
            return False
        return len(h & r) / max(1, len(h)) >= 0.5

    return [h for h in unique if not _too_similar(h)]


def format_followup_line(hints: list[str]) -> str:
    """Render hints as one terse REPL line. Empty list → empty string."""
    if not hints:
        return ""
    # 💡 marker + hints separated by " / "
    return "💡 " + "  ·  ".join(hints)
