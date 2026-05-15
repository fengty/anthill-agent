"""Score an agent's response against a task's expected style.

Crude on purpose. The goal isn't to grade content quality — it's to give a
clear, mechanical signal of whether the response matches the task type's
expectations. Length is a proxy: terse tasks want brevity, verbose tasks
want detail.

Real benchmarks for downstream releases can plug in an LLM-judge here.
For now, mechanical is more reproducible.
"""

from __future__ import annotations

import re

from anthill.bench.tasks import Task


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def score(task: Task, response: str) -> float:
    """Return [0, 1]. 1.0 = response style matches the task type."""
    if not response.strip():
        return 0.0
    n = word_count(response)
    if task.task_type == "terse":
        return 1.0 if n <= task.target_max_words else 0.0
    if task.task_type == "verbose":
        return 1.0 if n >= task.target_min_words else 0.0
    return 0.0
