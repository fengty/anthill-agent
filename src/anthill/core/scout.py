"""The Scout — the agent that decomposes natural-language tasks.

A real ant colony has scouts: workers whose only job is to go out, find
something worth doing, and come back with a plan. In Anthill, the Scout
plays the same role.

When a user says "translate this PDF and summarize the result", the Scout:
    1. Recognizes that this is two subtasks chained together.
    2. Names each subtask with a stable task_type label (translate, summarize).
    3. Decides whether they run sequentially or in parallel.
    4. Hands the plan back to the colony, which routes each subtask via
       pheromone trails.

The Scout itself uses an LLM, because natural-language decomposition is
exactly the thing LLMs are good at. The output is a structured plan, not
free text.

Two design choices worth flagging:

- The Scout returns JSON. We parse it strictly. If the model returns
  malformed JSON, we surface the error rather than guessing — the failure
  is informative and easy to fix prompt-side.
- task_type labels are not from a closed vocabulary. The Scout invents
  them based on the task, and the pheromone map accumulates trails for
  whatever labels it sees. Over time, a colony's task_type vocabulary
  itself becomes a fingerprint of what it does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from anthill.models import get_provider


SCOUT_SYSTEM_PROMPT = """You are the Scout for an agent colony.

A user gives you one request in natural language. Your job is to break it
into one or more concrete subtasks the colony can execute.

For each subtask, produce:
    - task_type: a short snake_case label that names what kind of work this is
                 (examples: translate, summarize, code_review, draft_email).
                 Reuse the same label for similar work — the colony tracks
                 expertise by label.
    - prompt:    the actual instruction the worker agent will receive.
                 Make it self-contained; the worker has no context beyond it.
    - depends_on: an optional list of task_type strings this subtask waits on.
                  Use this to express ordering.

Return ONLY a JSON object with this shape, no prose:

{
  "plan": [
    {"task_type": "<label>", "prompt": "<instruction>", "depends_on": []}
  ]
}

Rules:
- Prefer fewer, larger subtasks over many tiny ones.
- If the request is genuinely one task, return a single subtask.
- Keep task_type labels short and reusable.
- Never include explanations outside the JSON.
"""


@dataclass
class Subtask:
    task_type: str
    prompt: str
    depends_on: list[str]


@dataclass
class Plan:
    subtasks: list[Subtask]

    def __len__(self) -> int:
        return len(self.subtasks)


class Scout:
    """Decomposes natural-language requests into typed subtasks."""

    def __init__(self, model: str = "deepseek-chat") -> None:
        self.model = model

    async def plan(self, request: str) -> Plan:
        provider = get_provider(self.model)
        response = await provider.complete(
            request,
            system=SCOUT_SYSTEM_PROMPT,
            temperature=0.2,  # decomposition wants determinism, not creativity
        )
        return self._parse(response.text)

    @staticmethod
    def _parse(text: str) -> Plan:
        # Models sometimes wrap JSON in ```json ... ``` fences. Strip them.
        cleaned = text.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, flags=re.DOTALL)
        if fence:
            cleaned = fence.group(1)

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Scout returned non-JSON output: {text}") from e

        raw_plan = payload.get("plan")
        if not isinstance(raw_plan, list) or not raw_plan:
            raise RuntimeError(f"Scout plan is empty or wrong shape: {payload}")

        subtasks: list[Subtask] = []
        for entry in raw_plan:
            if not isinstance(entry, dict):
                raise RuntimeError(f"Scout subtask is not an object: {entry}")
            task_type = entry.get("task_type")
            prompt = entry.get("prompt")
            depends_on = entry.get("depends_on", []) or []
            if not isinstance(task_type, str) or not task_type.strip():
                raise RuntimeError(f"Scout subtask missing task_type: {entry}")
            if not isinstance(prompt, str) or not prompt.strip():
                raise RuntimeError(f"Scout subtask missing prompt: {entry}")
            subtasks.append(
                Subtask(
                    task_type=task_type.strip(),
                    prompt=prompt.strip(),
                    depends_on=list(depends_on),
                )
            )
        return Plan(subtasks=subtasks)
