"""The Scout — the agent that decomposes natural-language tasks.

Real ants have scouts: workers whose only job is to go out, find something
worth doing, and come back with a plan. In Anthill, the Scout plays the
same role.

When a user says "translate this PDF and summarize the result", the Scout:
    1. Recognizes that this is two subtasks chained together.
    2. Names each subtask with a stable task_type label (translate, summarize).
    3. Decides whether they run sequentially or in parallel.
    4. Hands the plan back to the nation, which routes each subtask via
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
  whatever labels it sees. Over time, a nation's task_type vocabulary
  itself becomes a fingerprint of what it does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from anthill.models import get_provider


SCOUT_SYSTEM_PROMPT_TEMPLATE = """You are the Scout for an agent nation.

A user (the king) gives you one request in natural language. Your job is
to plan how the nation will complete it — breaking the request into
concrete subtasks that depend on each other where needed.

For each subtask, produce:
    - task_type: a short snake_case label that names what kind of work this is
                 (examples: research, summarize, draft, review, translate).
                 Reuse labels — the nation tracks expertise by label.
    - prompt:    the actual instruction the worker will receive. Make it
                 self-contained; the worker only sees this prompt and whatever
                 the dependencies produced. Do NOT reference dependency outputs
                 by name in the prompt — they will be prepended automatically.
    - depends_on: list of task_type strings this subtask waits on. If your
                  plan has 'research' followed by 'summarize', summarize's
                  depends_on should be ["research"].

Return ONLY a JSON object with this shape, no prose:

{{
  "plan": [
    {{"task_type": "<label>", "prompt": "<instruction>", "depends_on": []}}
  ]
}}

Rules for good plans:
- Complex requests need multi-step plans. A research-and-write request
  should produce something like: research → outline → draft → polish.
- The LAST subtask should be the user-facing answer — the synthesis or
  final output the king will read. Earlier subtasks gather material.
- Keep each subtask to a single clear responsibility.
- Reuse task_types between subtasks only when they are doing the same
  KIND of work — otherwise prefer distinct labels.
- Never include explanations outside the JSON.

{vocabulary_section}"""


def build_system_prompt(known_task_types: list[str] | None = None) -> str:
    """Inject the nation's existing task-type vocabulary into the Scout prompt.

    Without this, the model invents a fresh label for every nuance of every
    request, and the pheromone map fragments. With it, the nation's existing
    expertise stays load-bearing.
    """
    if known_task_types:
        listing = ", ".join(known_task_types)
        section = (
            "This nation has existing expertise in these task types — strongly "
            "prefer reusing them when the work fits:\n"
            f"  {listing}\n"
            "Only invent a new task_type if no existing one is a good match."
        )
    else:
        section = "This nation has no prior task types yet. Choose carefully — labels you pick today become the nation's permanent vocabulary."
    return SCOUT_SYSTEM_PROMPT_TEMPLATE.format(vocabulary_section=section)


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

    async def plan(
        self,
        request: str,
        *,
        known_task_types: list[str] | None = None,
        episodic_context: str = "",
    ) -> Plan:
        provider = get_provider(self.model)
        # Episodic hints are placed in the user message rather than the
        # system prompt so they cannot be cached, and so Scout treats them
        # as case-by-case context rather than enduring rules.
        user_message = request
        if episodic_context:
            user_message = f"{episodic_context}\n\n---\n\nNew request:\n{request}"
        response = await provider.complete(
            user_message,
            system=build_system_prompt(known_task_types),
            temperature=0.2,
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
