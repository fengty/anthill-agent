"""Task pool with two types whose ground truth is mechanically checkable.

Why these two types?

We need tasks where:
    1. There's a clear right answer style (so scoring is objective).
    2. Different agents can be biased toward one style via system prompt.
    3. No LLM-judge is needed — a Python function can score it.

Length-based scoring fits perfectly. A "terse" task expects 1-3 words;
a "verbose" task expects 20+ words. Both have a clear floor and ceiling.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Task:
    """A single benchmark task."""

    task_type: str         # "terse" or "verbose"
    prompt: str
    target_min_words: int  # for "verbose"; ignored for "terse"
    target_max_words: int  # for "terse"; ignored for "verbose"


TERSE_PROMPTS = [
    "Capital of France?",
    "1 plus 2 equals?",
    "Largest planet?",
    "Color of grass?",
    "Opposite of hot?",
    "Sun rises in the?",
    "Number of legs on a spider?",
    "Square root of 16?",
    "Author of Hamlet?",
    "Chemical symbol for water?",
]

VERBOSE_PROMPTS = [
    "Explain what photosynthesis is.",
    "Describe how a bicycle works.",
    "Explain the water cycle in detail.",
    "Describe what causes thunder.",
    "Explain how electricity reaches our homes.",
    "Describe how seeds become trees.",
    "Explain how the human heart works.",
    "Describe what happens when we sleep.",
    "Explain how rainbows form.",
    "Describe how a refrigerator works.",
]


def generate_pool(n_terse: int = 25, n_verbose: int = 25, seed: int = 42) -> list[Task]:
    """Generate a shuffled task pool with the given counts."""
    rng = random.Random(seed)
    pool: list[Task] = []
    for _ in range(n_terse):
        pool.append(
            Task(
                task_type="terse",
                prompt=rng.choice(TERSE_PROMPTS),
                target_min_words=0,
                target_max_words=3,
            )
        )
    for _ in range(n_verbose):
        pool.append(
            Task(
                task_type="verbose",
                prompt=rng.choice(VERBOSE_PROMPTS),
                target_min_words=20,
                target_max_words=10_000,
            )
        )
    rng.shuffle(pool)
    return pool
