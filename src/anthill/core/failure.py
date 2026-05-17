"""Structured failure attribution — name the reason so it can drive action.

Before v0.5 a failed attempt was just "score 0.0 and an error string."
That's fine for retry logic, but useless for diagnosing *why* a citizen
keeps failing. A 5-citizen nation that's been hit by a provider's policy
filter looks identical to one running into rate limits or timeouts —
same outcome, different remedies.

This module classifies failures with rule-based heuristics on the
TaskResult output (we deliberately do NOT use an LLM to diagnose LLM
failures — that's circular when the failure mode is "LLM down").

Classification surfaces:
  - FailureReason: a stable enum the rest of the system can pattern-match on
  - classify_attempt(output, exception=None, response=None): the rule pipeline
  - explain(reason): one-line human description

Why rule-based not LLM-based:
  - Reliability when models fail
  - Predictable cost (zero, this runs at every attempt)
  - Testable: each rule maps to a regex/string pattern we can pin

The output is consumed by:
  - history.jsonl per-attempt 'failure_reason' field (v0.5.0)
  - lifecycle health check → quarantine trigger (v0.5.1+)
  - the user-facing `anthill history failures` report
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class FailureReason(str, Enum):
    """The structured cause for an attempt scoring 0.

    Inherits from str so JSON serialization is trivial — just store the
    enum value as-is and rehydrate with FailureReason(value).
    """

    EMPTY_RESPONSE = "empty_response"          # provider returned "" or whitespace
    POLICY_REFUSAL = "policy_refusal"          # content policy / "I cannot help"
    TIMEOUT = "timeout"                        # network or provider timeout
    NETWORK = "network"                        # connection error, DNS, 5xx
    RATE_LIMIT = "rate_limit"                  # 429 / "rate limit exceeded"
    AUTH = "auth"                              # 0.1.21: bad / missing API key, 401, login fail
    FORMAT_ERROR = "format_error"              # Scout JSON parse, schema mismatch
    MODEL_ERROR = "model_error"                # generic 4xx from provider, hallucinated tool call
    JUDGE_LOW = "judge_low"                    # judge gave a low score (not a hard failure)
    UNKNOWN = "unknown"                        # we couldn't classify it


# Pattern lists — kept dead-simple so a contributor can add a new
# trigger without learning a new system. Order matters: first match
# wins. Patterns are lowercased before matching.

_POLICY_REFUSAL_PATTERNS = (
    "i cannot", "i can't help", "i'm not able to",
    "i am not able to", "as an ai", "i'm sorry, but",
    "i apologize, but i cannot",
    "violates our content policy", "against my guidelines",
    "i won't be able to", "i must decline",
    "无法帮助", "不能回答", "无法回答", "拒绝", "违反", "政策",
)

_TIMEOUT_PATTERNS = (
    "timeout", "timed out", "deadline exceeded",
    "read timed out", "request timed out",
)

_NETWORK_PATTERNS = (
    "connection error", "connection refused", "connection reset",
    "name or service not known", "no route to host",
    "ssl error", "ssl handshake", "tls",
    "dns", "name resolution",
    "503", "502", "504",
    "remote end closed", "broken pipe",
    # 0.1.20 — proxy / transport config issues. Surfacing these as
    # NETWORK so the retry log says "(network)" instead of "(unknown)"
    # — and so the user knows the bug is in their proxy setup, not
    # the model.
    "socks proxy", "socksio",
    "proxy", "no proxy",
)

_RATE_LIMIT_PATTERNS = (
    "rate limit", "too many requests", "429",
    "quota exceeded", "tokens per minute", "request per minute",
)

# 0.1.21 — auth-related failures. Catches the case from the real-user
# report: a citizen has model="minimax" but no ModelEntry / no key
# loaded, so MiniMax returns "error 1004: login fail". Same bucket
# covers OpenAI 401, Anthropic invalid_api_key, etc. Goes BEFORE
# RATE_LIMIT in match order via classify_attempt's ordering below.
_AUTH_PATTERNS = (
    "401", "unauthorized",
    "invalid api key", "invalid_api_key",
    "incorrect api key",
    "api key not found", "api key required",
    "missing api key", "no api key",
    "authentication failed", "authentication error",
    "login fail", "error 1004",   # MiniMax-style
    "carry the api secret key",   # MiniMax's exact wording
    "invalid x-api-key",
)

_FORMAT_ERROR_PATTERNS = (
    "json", "expecting value", "invalid syntax",
    "malformed", "unparseable", "could not parse",
)

_MODEL_ERROR_PATTERNS = (
    "400 ", "401 ", "403 ", "404 ", "422 ",
    "invalid request", "model error", "model unavailable",
    "model not found", "invalid model",
)


def _any_match(haystack: str, patterns: tuple[str, ...]) -> bool:
    return any(p in haystack for p in patterns)


def classify_attempt(
    output: str,
    *,
    exception: Optional[BaseException] = None,
    success_score: float = 0.0,
    judge_score: float | None = None,
) -> FailureReason | None:
    """Return the most specific FailureReason for a non-success attempt.

    Returns None when the attempt actually succeeded (success_score > 0
    AND output non-empty AND no exception) — callers should not store a
    "reason" on successful attempts.

    Classification order is intentional — more specific rules win:
      1. Empty output (regardless of why)
      2. Exception-class direct mapping (Timeout, Connection)
      3. Body text patterns: policy / rate-limit / network / timeout / format / model
      4. judge_score < 0.3 with otherwise-OK attempt → JUDGE_LOW
      5. Fallback: UNKNOWN
    """
    text = (output or "").strip()
    if not text:
        # Treat empty output as the most specific failure mode regardless
        # of whether an exception was raised — the user-facing symptom is
        # the same and the remedy is the same (try another citizen).
        if exception is None and success_score > 0:
            return None
        return FailureReason.EMPTY_RESPONSE

    # Exception-class shortcuts — robust across providers because the
    # exception types come from httpx / asyncio rather than free-text.
    if exception is not None:
        cls_name = type(exception).__name__.lower()
        if "timeout" in cls_name:
            return FailureReason.TIMEOUT
        if "connect" in cls_name or "transport" in cls_name:
            return FailureReason.NETWORK
        # Fall through to body text for less-obvious exception classes.

    haystack = text.lower()

    # Auth comes BEFORE rate-limit/model-error: "401 Unauthorized" is
    # ambiguous between those buckets without this priority. The user-
    # facing remedy is also distinct ("check your key", not "wait").
    if _any_match(haystack, _AUTH_PATTERNS):
        return FailureReason.AUTH
    if _any_match(haystack, _RATE_LIMIT_PATTERNS):
        return FailureReason.RATE_LIMIT
    if _any_match(haystack, _POLICY_REFUSAL_PATTERNS):
        return FailureReason.POLICY_REFUSAL
    if _any_match(haystack, _TIMEOUT_PATTERNS):
        return FailureReason.TIMEOUT
    if _any_match(haystack, _NETWORK_PATTERNS):
        return FailureReason.NETWORK
    if _any_match(haystack, _MODEL_ERROR_PATTERNS):
        return FailureReason.MODEL_ERROR
    if haystack.startswith("[error]") and _any_match(haystack, _FORMAT_ERROR_PATTERNS):
        return FailureReason.FORMAT_ERROR

    # Soft-fail path: judge thought the output was bad even though it
    # didn't error out. Useful for the immune system.
    if judge_score is not None and judge_score < 0.3 and success_score > 0:
        return FailureReason.JUDGE_LOW

    # If success_score > 0 and nothing matched, this was probably fine.
    if success_score > 0 and exception is None:
        return None

    return FailureReason.UNKNOWN


def explain(reason: FailureReason) -> str:
    """One-line human description. Stable across versions."""
    return {
        FailureReason.EMPTY_RESPONSE: "provider returned an empty response",
        FailureReason.POLICY_REFUSAL: "model refused on policy / safety grounds",
        FailureReason.TIMEOUT: "request timed out",
        FailureReason.NETWORK: "network or transport error",
        FailureReason.RATE_LIMIT: "provider rate limit hit",
        FailureReason.AUTH: "API key missing or invalid for this citizen's model",
        FailureReason.FORMAT_ERROR: "output did not match expected format",
        FailureReason.MODEL_ERROR: "provider returned an API error",
        FailureReason.JUDGE_LOW: "output passed but judge scored it low",
        FailureReason.UNKNOWN: "could not be classified automatically",
    }[reason]


def is_actionable(reason: FailureReason) -> bool:
    """Whether this failure should bias quarantine logic.

    Some failures (RATE_LIMIT, NETWORK) are environmental — the citizen
    isn't broken. Others (POLICY_REFUSAL, EMPTY_RESPONSE, MODEL_ERROR)
    suggest the citizen specifically is having trouble and should be
    isolated. UNKNOWN/JUDGE_LOW are middle ground — they count but
    less heavily.
    """
    return reason in {
        FailureReason.POLICY_REFUSAL,
        FailureReason.EMPTY_RESPONSE,
        FailureReason.MODEL_ERROR,
        FailureReason.FORMAT_ERROR,
    }


__all__ = [
    "FailureReason",
    "classify_attempt",
    "explain",
    "is_actionable",
]
