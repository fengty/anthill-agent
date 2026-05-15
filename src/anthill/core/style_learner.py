"""Mine rated exemplars into a house-style suggestion.

The king's thumbs-up and thumbs-down are signals: with enough of them,
we can ask an LLM to read the pile and propose a paragraph that captures
the implicit preferences. The user then edits or accepts the suggestion.

We do not auto-apply. A house style is the soul of the nation —
overwriting it without consent is wrong. The flow is:

    anthill style learn   →  suggested.md printed to stdout
    user reviews          →  copy into actual house_style.md if they like it

When there are not enough exemplars (default minimum: 3), we say so
honestly rather than fabricate a style out of thin air.
"""

from __future__ import annotations

from anthill.core.feedback import Exemplar
from anthill.models import get_provider


STYLE_SYSTEM_PROMPT = """You are a style anthropologist.

You will be given examples of outputs the king APPROVED and examples the
king REJECTED, along with the requests that produced them. Your job is
to infer the style preferences these ratings reveal and write a short,
crisp house style description that future workers should follow.

Output ONLY the house style guidance, as a markdown list of 3-7 concrete
rules. No preamble, no explanation, no apologies. Each rule should be a
single sentence the worker can act on.

If the evidence is contradictory or thin, say so honestly in one line at
the end:

> Note: <what's unclear or limited>

Example output:

- Prefer answers under 50 words; expand only when asked.
- Use code examples for any technical concept.
- Avoid bullet points unless the user's request explicitly asks for a list.

Now read the exemplars and write the house style."""


def format_exemplars_for_prompt(exemplars: list[Exemplar]) -> str:
    lines: list[str] = []
    approved = [e for e in exemplars if e.rating == "up"]
    rejected = [e for e in exemplars if e.rating == "down"]

    if approved:
        lines.append("APPROVED outputs (the king liked these):\n")
        for i, e in enumerate(approved, start=1):
            lines.append(f"Request {i}: {e.request}")
            lines.append(f"Output {i}:\n{e.output.strip()}\n")
    if rejected:
        lines.append("REJECTED outputs (the king did not like these):\n")
        for i, e in enumerate(rejected, start=1):
            lines.append(f"Request {i}: {e.request}")
            lines.append(f"Output {i}:\n{e.output.strip()}\n")
    return "\n".join(lines)


async def suggest_house_style(
    exemplars: list[Exemplar],
    *,
    model: str = "deepseek-chat",
    min_exemplars: int = 3,
) -> str:
    """Return a suggested house_style markdown blob, or a clear refusal.

    The refusal path is important — fabricating style guidance from one
    thumbs-up is worse than honestly saying "I don't have enough."
    """
    if len(exemplars) < min_exemplars:
        return (
            f"Not enough exemplars to suggest a house style yet. "
            f"Have {len(exemplars)}, need {min_exemplars}. "
            f"Rate a few more `anthill ask` results with "
            f"`anthill rate up/down` first."
        )

    provider = get_provider(model)
    prompt = format_exemplars_for_prompt(exemplars)
    response = await provider.complete(
        prompt,
        system=STYLE_SYSTEM_PROMPT,
        temperature=0.3,
    )
    return response.text.strip()
