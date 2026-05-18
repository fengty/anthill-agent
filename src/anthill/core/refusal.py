"""0.1.40 — detect "the citizen punted the work back to the king."

Anthill's core narrative: **用户是国王，子民通过多模型能力，想尽办法
完成用户的任务**. A citizen that says "please paste the content" /
"I can't access that link" / "could you provide more details" has
NOT served the king — it just deferred. That's worse than a content-
policy refusal, because nothing was actually tried.

This module distinguishes two refusal classes that ``core/failure.py``
already half-handles:

  - ``POLICY_REFUSAL`` — model said no on safety / policy grounds.
    Don't retry; the answer IS the refusal. Already in failure.py.

  - ``USER_SERVING_REFUSAL`` (new): model gave up and asked the
    king to do the work. Citizens should have tried harder. We
    detect these and DOWNGRADE success_score so the executor's
    retry path kicks in — ideally with a different citizen who
    might try a different approach. Plus the deliberation loop
    (when active) sees a low quality score and runs another round
    with a "be more resourceful" addendum to the critic.

Conservative patterns by design. False positives are the bigger
risk than false negatives: erroneously retrying a substantive
answer wastes tokens; missing a refusal just means the user lives
with the lazy answer this one time.
"""

from __future__ import annotations

import re


# Patterns that strongly indicate "I'm bouncing this back to you."
# These are *anchored* to the start of a sentence (^ or after a
# sentence-ending punctuation) so an incidental "please provide a
# concrete example next time" mid-paragraph doesn't fire.
# Sentence leaders. Includes the Chinese comma (，) because Chinese
# response style often glues short clauses together with "，" rather
# than "。" — e.g. "抱歉，我无法访问..." has the deferral clause
# attached after a comma. Treating "，" as a sentence boundary is a
# small false-positive risk we accept for the higher hit rate.
_LEADER = r"(?:^|[.;!?。；！？，]\s*|\n)"

# Build the master pattern. Two-tier:
#   * STRONG indicators — single hit is enough to flag
#   * SOFT indicators — need at least one + a second supporting cue
# Kept as simple text matches inside the regex; less general than
# semantic detection but reliably testable.

_STRONG_REFUSAL_PATTERNS: tuple[str, ...] = (
    # English — direct passes-the-work-back phrasing
    r"please\s+(?:paste|provide|share|copy|attach)\s+",
    r"could\s+you\s+(?:paste|provide|share|copy)\s+",
    r"please\s+(?:provide|share)\s+(?:more\s+)?(?:details?|information|context)",
    r"i\s+(?:cannot|can'?t|am\s+unable\s+to|don'?t\s+have\s+the\s+ability\s+to)\s+(?:access|browse|open|fetch|read|view)\s+",
    r"i\s+don'?t\s+have\s+(?:direct\s+)?access\s+to\s+",
    r"i\s+am\s+unable\s+to\s+(?:access|browse|open|fetch)\s+",
    # Contracted forms: "I'm unable to access", "I'm not able to access"
    r"i'?m\s+(?:unable|not\s+able)\s+to\s+(?:access|browse|open|fetch|read|view|directly)\s+",
    r"i'?m\s+(?:unable|not\s+able)\s+to\s+(?:directly\s+)?(?:access|browse|open|fetch|read|view)\s+",
    # "If you could share / paste / send"
    r"if\s+you\s+could\s+(?:share|paste|send|provide|copy)\s+",
    r"i'?m\s+sorry,?\s+(?:but\s+)?i\s+(?:cannot|can'?t)\s+(?:access|browse|fetch|open|view)\s+",
    # Chinese
    r"请(?:直接)?(?:粘贴|发送|提供|分享|告诉|发我|提供)",
    r"麻烦(?:你)?(?:提供|粘贴|分享)",
    r"我(?:无法|不能|没办法)(?:直接)?(?:访问|打开|浏览|获取|读取)",
    r"我(?:没有|不具备)(?:直接|访问)?(?:链接|网络|网页|外部)的?(?:能力|权限)",
    r"如果你?能(?:提供|分享|粘贴)",
)


_SOFT_REFUSAL_PATTERNS: tuple[str, ...] = (
    # Hedging that, by itself, isn't a refusal — but combined with a
    # strong cue suggests the model is stalling.
    r"i\s+would\s+need\s+(?:more|the)\s+(?:content|details|information)",
    r"without\s+(?:access\s+to|seeing)\s+(?:the|that|this)",
    r"unable\s+to\s+(?:directly|external)",
    r"我需要更多(?:的)?(?:信息|内容|详情|上下文)",
    r"如果(?:你|您)能(?:提供|给我)",
)


_STRONG_RE = re.compile(
    _LEADER + "(?:" + "|".join(_STRONG_REFUSAL_PATTERNS) + ")",
    re.IGNORECASE,
)
_SOFT_RE = re.compile(
    _LEADER + "(?:" + "|".join(_SOFT_REFUSAL_PATTERNS) + ")",
    re.IGNORECASE,
)


def is_user_serving_refusal(text: str) -> bool:
    """True when the citizen bounced the work back to the king.

    Two trip conditions:
      1. Any strong-pattern match (cheap definitive case)
      2. Multiple soft-pattern matches with no substantive content
         around them (defensive against models that pad a refusal
         with hedging instead of a clear "I can't")

    Empty / very-short responses default to False here — the
    EMPTY_RESPONSE classifier already handles those.
    """
    if not text or len(text.strip()) < 20:
        return False
    if _STRONG_RE.search(text):
        return True
    soft_hits = len(_SOFT_RE.findall(text))
    if soft_hits >= 2:
        return True
    return False


# Suffix appended to a worker's system prompt on the retry attempt
# after a refusal was detected. Calibrated to nudge "try harder"
# without overriding the worker's safety guardrails — we don't want
# to bypass POLICY_REFUSAL territory by mistake.
RESOURCEFUL_RETRY_ADDENDUM = (
    "\n\n[Note: a previous attempt at this task bounced the work back "
    "to the user (\"please paste\" / \"I can't access\"). Don't do that. "
    "Produce the best substantive answer the available context supports — "
    "infer from URL patterns / domain knowledge / general reasoning. If "
    "a specific piece of input is truly needed, ask for it in ONE short "
    "sentence at the END, after delivering whatever analysis you can. "
    "Lead with the analysis, not the disclaimer.]"
)
