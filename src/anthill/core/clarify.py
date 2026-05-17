"""Clarification turn — ask the user before guessing.

Before v0.9 every ask went straight from user input to Scout to
execution. For sharp requests that's fine; for vague ones it's
"garbage in, garbage out" — Scout makes a fragile plan based on
guesses about what the user meant, and the work cascades from there.

This module introduces a **clarification turn**: a quick LLM-driven
check that asks "is this request specific enough to plan against?"
and, when the answer is no, produces 1-3 short questions for the
user. The user's answers get merged into the original request before
Scout sees anything.

What makes this richer than "every loop framework should clarify":
  - **It's the same mechanism, not a special case.** The clarifier
    is just another role; the model decides what to ask.
  - **Opt-out, not opt-in.** Users who want fast direct execution
    can disable it via policy. The default is "ask only when
    complexity ≥ normal AND the model thinks it's genuinely
    ambiguous", which keeps trivial asks (greetings) untouched.
  - **One turn cap.** A clarifier that re-asks after the user
    answered is annoying. We merge once and proceed even if the
    answer is still vague.
  - **User can /skip.** If the user wants to commit to "do your
    best," `skip` is a first-class response that means "no further
    clarification, execute as-is."

The clarifier itself runs as a Nation task with task_type="clarify",
so a citizen good at clarifying naturally emerges via pheromone
trails over time — same emergent-specialization story as any other
role.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from anthill.core.nation import Nation


_CLARIFY_SYSTEM_PROMPT = """You are a clarifier for an agent nation.

The user sent a request. Your job: decide whether it's specific enough
for the nation to plan and execute against, or whether 1-3 quick
questions would dramatically improve the result.

Examples of requests that NEED clarification:
  "write me something"                  — type? audience? length?
  "help with my project"                — what kind? what's stuck?
  "translate this"                      — what's "this"? to what language?
  "improve the design"                  — design of what? for what users?

Examples that DON'T need clarification (just execute):
  "translate hello to French"           — clear
  "what's 2+2"                          — clear
  "summarize this PDF in three bullets" — clear (assuming file in context)
  "research the top 3 vector DBs"       — clear

Return ONLY a JSON object, no prose, no code fences:

{
  "clear": <true | false>,
  "questions": ["<short question>", ...],   // empty list if clear:true
  "why": "<one short sentence explaining what's missing — only when clear:false>"
}

Rules:
- At most 3 questions. Pick the ones that will most change the plan.
- Questions short, specific, single-clause. No essays.
- Bias toward "clear: true" — a clarifier that asks every time is
  worse than no clarifier. Only flag genuinely ambiguous requests.
"""


@dataclass
class ClarificationQuestions:
    """Output of the clarifier when it thinks the request is too vague."""

    questions: list[str]
    why: str

    @property
    def is_empty(self) -> bool:
        return not self.questions


# What the CLI/REPL hands back: either the user's answer (free text) or
# None to mean "skip clarification, proceed as-is."
# Uses Optional[str] (not str | None) so the assignment evaluates at
# import time under Python 3.9 too — PEP 604 syntax only works in
# annotations under `from __future__ import annotations`.
ClarifyHandler = Callable[[ClarificationQuestions], Awaitable[Optional[str]]]


def _parse_response(text: str) -> ClarificationQuestions | None:
    """Strict JSON parse. Returns None if request is clear or unparseable.

    Same defensive parsing pattern as scout._try_parse_strict: prefer
    "no clarification" over a hallucinated question when the model
    misbehaves.
    """
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, flags=re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        cleaned = match.group(0)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    # Treat anything other than explicit `clear: false` as "clear" — bias
    # toward not annoying the user.
    if data.get("clear", True) is not False:
        return None

    raw_qs = data.get("questions") or []
    if not isinstance(raw_qs, list):
        return None
    questions: list[str] = []
    for q in raw_qs[:3]:  # hard cap at 3
        if isinstance(q, str):
            q = q.strip()
            if q:
                questions.append(q)
    if not questions:
        return None
    why = str(data.get("why", "")).strip() or "the request could be more specific"
    return ClarificationQuestions(questions=questions, why=why)


async def assess_clarity(
    nation: "Nation",
    request: str,
    *,
    model: str | None = None,
) -> ClarificationQuestions | None:
    """Run the clarifier on `request`. Returns questions or None if clear.

    Uses task_type='clarify' so the router can develop pheromone trails
    for whoever's good at this role over time (same mechanism as
    'review' in deliberation, or 'general' as a default).

    Returns None on any failure path — if the clarifier provider is
    down, we'd rather skip clarification than block the user. The
    cost of a missed clarification is "result is slightly off"; the
    cost of a busted clarifier is "the whole ask hangs."
    """
    # Wrap the request so prompt-injection in user input can't pretend
    # to be the clarifier's instructions.
    wrapped = f"<user_request>\n{request}\n</user_request>"
    try:
        result = await nation.run(
            "clarify",
            wrapped + "\n\n" + _CLARIFY_SYSTEM_PROMPT,
        )
    except Exception:  # noqa: BLE001 — clarifier is best-effort
        return None
    text = str(result.output).strip()
    if not text:
        return None
    return _parse_response(text)


def merge_answers(original_request: str, user_response: str) -> str:
    """Fold the user's answers back into the request for Scout to see.

    We don't try to match each answer to each question — the user may
    answer in any shape (numbered list, free prose, "all three are
    yes"). The model that runs Scout will read the merged blob and
    figure out the structure itself.
    """
    user_response = user_response.strip()
    if not user_response:
        return original_request
    return (
        f"{original_request.strip()}\n\n"
        f"[User's clarification: {user_response}]"
    )


# --- the clarification loop -----------------------------------------------


async def maybe_clarify(
    nation: "Nation",
    request: str,
    on_clarify: ClarifyHandler | None,
) -> str:
    """If clarifier flags ambiguity AND a handler is provided, ask the user.

    Returns the (possibly merged) request to pass to Scout. If no
    handler is provided, OR clarifier says clear, OR user skipped,
    the original request is returned unchanged.

    Cap: at most ONE clarification turn per ask. Re-clarifying gets
    annoying fast and the second round rarely helps.
    """
    if on_clarify is None:
        return request

    questions = await assess_clarity(nation, request)
    if questions is None:
        return request

    user_answer = await on_clarify(questions)
    if user_answer is None or not user_answer.strip():
        # User skipped (e.g. /skip in REPL) — proceed with the original.
        return request

    return merge_answers(request, user_answer)


__all__ = [
    "ClarificationQuestions",
    "ClarifyHandler",
    "assess_clarity",
    "merge_answers",
    "maybe_clarify",
]
