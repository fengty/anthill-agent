"""v0.8.1 — task complexity classification.

Three things under test:
  1. fast_classify rule fidelity — greetings = trivial, multi-step verbs
     = complex, ambiguous = None
  2. Scout JSON parsing reads the complexity field with sane fallback
  3. Nation.ask + deliberation short-circuit on trivial
"""

from __future__ import annotations

import pytest

from anthill.core.complexity import (
    Complexity,
    deliberation_default,
    description,
    fast_classify,
)
from anthill.core.scout import Plan, Scout


# --- fast_classify rules -------------------------------------------------


@pytest.mark.parametrize(
    "msg,expected",
    [
        # Single-word greetings / acks
        ("hi", "trivial"),
        ("hello", "trivial"),
        ("Hey!", "trivial"),
        ("thanks", "trivial"),
        ("ok", "trivial"),
        ("你好", "trivial"),
        ("谢谢", "trivial"),
        ("再见", "trivial"),
        # Short non-marker requests
        ("what is 2+2", "trivial"),
        ("the time?", "trivial"),
        # Empty
        ("", "trivial"),
        ("   ", "trivial"),
    ],
)
def test_fast_classify_trivial(msg: str, expected: Complexity) -> None:
    assert fast_classify(msg) == expected


@pytest.mark.parametrize(
    "msg",
    [
        "research the top 3 LLMs",
        "compare A and B",
        "write a summary of the meeting",
        "draft an email to the team",
        "analyze why my code is slow",
        "summarize this PDF",
        "translate this to English",
        "review my pull request",
        # Chinese variants
        "调研一下这个项目",
        "分析这段代码",
        "翻译成英文",
        "撰写一份提案",
        "比较 A 和 B",
        # Long-form
        "I have a question about the API. First, how do I authenticate? "
        "Second, what's the rate limit? Third, what are the error codes? "
        "Fourth, how do I handle retries?",
    ],
)
def test_fast_classify_complex(msg: str) -> None:
    assert fast_classify(msg) == "complex"


@pytest.mark.parametrize(
    "msg",
    [
        # Medium-length, no markers, no obvious greeting — let Scout decide.
        # Must be > 5 words AND have no complex punctuation, otherwise
        # the heuristic confidently picks 'trivial'.
        "I think the situation is more nuanced than that here",
        "How does this code thing actually work in practice today",
    ],
)
def test_fast_classify_ambiguous_returns_none(msg: str) -> None:
    """Conservative — return None when not confident."""
    assert fast_classify(msg) is None


def test_short_question_is_trivial_not_ambiguous() -> None:
    """5-word direct questions without complex markers ARE trivial.

    'Tell me about photosynthesis briefly' = one prompt, one LLM call.
    No need for multi-step planning."""
    assert fast_classify("Tell me about photosynthesis briefly") == "trivial"


def test_complex_marker_beats_short_length() -> None:
    """A 2-word 'research X' shouldn't fall into trivial just because short."""
    assert fast_classify("research stigmergy") == "complex"


def test_trivial_with_punctuation_still_trivial() -> None:
    assert fast_classify("Hi!") == "trivial"
    assert fast_classify("hello?") == "trivial"


# --- policy + description -----------------------------------------------


def test_deliberation_default_only_for_complex() -> None:
    assert deliberation_default("complex") is True
    assert deliberation_default("normal") is False
    assert deliberation_default("trivial") is False


def test_description_returns_human_string() -> None:
    assert "trivial" in description("trivial")
    assert "deliberation" in description("complex")


# --- Scout JSON parsing reads complexity --------------------------------


def test_scout_parses_complexity_field() -> None:
    text = """
    {
        "plan": [
            {"task_type": "research", "prompt": "dig", "depends_on": []}
        ],
        "complexity": "complex"
    }
    """
    plan = Scout._parse(text)
    assert plan.complexity == "complex"


def test_scout_complexity_defaults_to_normal_when_missing() -> None:
    text = '{"plan": [{"task_type": "x", "prompt": "y", "depends_on": []}]}'
    plan = Scout._parse(text)
    assert plan.complexity == "normal"


def test_scout_complexity_normalizes_invalid_value() -> None:
    """Hallucinated 'medium' / 'easy' falls back to normal."""
    text = (
        '{"plan": [{"task_type": "x", "prompt": "y", "depends_on": []}],'
        ' "complexity": "medium"}'
    )
    plan = Scout._parse(text)
    assert plan.complexity == "normal"


def test_scout_complexity_case_insensitive() -> None:
    text = (
        '{"plan": [{"task_type": "x", "prompt": "y", "depends_on": []}],'
        ' "complexity": "COMPLEX"}'
    )
    plan = Scout._parse(text)
    assert plan.complexity == "complex"


def test_plan_dataclass_default_complexity_is_normal() -> None:
    """For backwards-compat with code paths that build Plan() manually."""
    from anthill.core.scout import Subtask
    p = Plan(subtasks=[Subtask("x", "y", [])])
    assert p.complexity == "normal"


# --- Nation.ask fast-path -----------------------------------------------


@pytest.mark.asyncio
async def test_nation_ask_skips_scout_on_trivial(monkeypatch) -> None:
    """Trivial requests should NOT call Scout — saves a round trip."""
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Scout as ScoutClass

    scout_called = False

    async def fake_plan(self, *args, **kwargs):  # noqa: ANN001, ANN201
        nonlocal scout_called
        scout_called = True
        from anthill.core.scout import Plan as _Plan, Subtask as _Sub
        return _Plan(subtasks=[_Sub("x", "y", [])])

    monkeypatch.setattr(ScoutClass, "plan", fake_plan)

    n = Nation(name="t")
    a = Agent(id="ant-1", model="x")
    n.agents = [a]

    async def fake_run(task_type, prompt, *, forbid=None):  # noqa: ANN001, ANN201
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="hello back", success_score=1.0, duration_seconds=0.0,
        )

    n.run = fake_run  # type: ignore[assignment]
    result = await n.ask("hi")
    assert scout_called is False, "Scout should be skipped for trivial requests"
    assert result.plan.complexity == "trivial"
    assert result.outcomes[0].status == "ok"


@pytest.mark.asyncio
async def test_nation_ask_uses_scout_on_complex(monkeypatch) -> None:
    from anthill.core.agent import Agent, TaskResult
    from anthill.core.nation import Nation
    from anthill.core.scout import Scout as ScoutClass

    scout_called = False

    async def fake_plan(self, *args, **kwargs):  # noqa: ANN001, ANN201
        nonlocal scout_called
        scout_called = True
        from anthill.core.scout import Plan as _Plan, Subtask as _Sub
        return _Plan(
            subtasks=[_Sub("research", "dig", [])],
            complexity="normal",
        )

    monkeypatch.setattr(ScoutClass, "plan", fake_plan)

    n = Nation(name="t")
    a = Agent(id="ant-1", model="x")
    n.agents = [a]

    async def fake_run(task_type, prompt, *, forbid=None):  # noqa: ANN001, ANN201
        return TaskResult(
            task_id="t", agent_id="ant-1", task_type=task_type,
            output="findings", success_score=1.0, duration_seconds=0.0,
        )

    n.run = fake_run  # type: ignore[assignment]
    result = await n.ask("research the top 3 LLMs")
    assert scout_called is True
    # fast_classify detects "research" → complex; should override Scout's normal
    assert result.plan.complexity == "complex"


# --- Deliberation short-circuits on trivial -----------------------------


@pytest.mark.asyncio
async def test_deliberate_skips_on_trivial_complexity() -> None:
    """Even if quality is low, trivial requests get one round only."""
    from dataclasses import dataclass, field
    from anthill.core.deliberate import deliberate

    @dataclass
    class _FakeAttempt:
        success_score: float = 0.3
        scores: dict = field(default_factory=lambda: {"q": 0.3})

    @dataclass
    class _FakeOutcome:
        status: str = "ok"
        attempts: list = field(default_factory=list)

    @dataclass
    class _FakePlan:
        subtasks: list = field(default_factory=list)
        complexity: str = "trivial"

        def __len__(self) -> int:
            return len(self.subtasks)

    @dataclass
    class _FakeResult:
        request: str = "hi"
        plan: object = None  # set below
        outcomes: list = field(default_factory=list)
        budget: object = None
        final_output: str = "hello"

    class _Nation:
        run_called = 0

        async def ask(self, request, **kwargs):  # noqa: ANN001, ANN201
            return _FakeResult(
                request=request,
                plan=_FakePlan(subtasks=["x"], complexity="trivial"),
                outcomes=[_FakeOutcome(attempts=[_FakeAttempt()])],
            )

        async def run(self, *args, **kwargs):  # noqa: ANN001, ANN201
            _Nation.run_called += 1
            raise AssertionError("critic should NOT be called for trivial")

    result = await deliberate(_Nation(), "hi", quality_threshold=0.99)  # type: ignore[arg-type]
    assert result.total_rounds == 1
    assert result.stop_reason == "trivial"
    assert _Nation.run_called == 0


@pytest.mark.asyncio
async def test_deliberate_proceeds_for_non_trivial_low_quality() -> None:
    """Sanity: complex/normal plans WITH low quality DO trigger critique."""
    from dataclasses import dataclass, field
    from anthill.core.deliberate import deliberate

    @dataclass
    class _FakeAttempt:
        success_score: float = 0.5
        scores: dict = field(default_factory=lambda: {"q": 0.5})

    @dataclass
    class _FakeOutcome:
        status: str = "ok"
        attempts: list = field(default_factory=list)

    @dataclass
    class _FakePlan:
        subtasks: list = field(default_factory=list)
        complexity: str = "normal"

        def __len__(self) -> int:
            return len(self.subtasks)

    @dataclass
    class _FakeResult:
        request: str = "x"
        plan: object = None
        outcomes: list = field(default_factory=list)
        budget: object = None
        final_output: str = "draft"

    class _Nation:
        critic_called = False

        async def ask(self, request, **kwargs):  # noqa: ANN001, ANN201
            return _FakeResult(
                request=request,
                plan=_FakePlan(subtasks=["x"], complexity="normal"),
                outcomes=[_FakeOutcome(attempts=[_FakeAttempt()])],
            )

        async def run(self, task_type, prompt, **kwargs):  # noqa: ANN001, ANN201, ARG002
            _Nation.critic_called = True
            from anthill.core.agent import TaskResult
            return TaskResult(
                task_id="t", agent_id="ant-x", task_type=task_type,
                output="critique text", success_score=1.0, duration_seconds=0.0,
            )

    await deliberate(
        _Nation(), "research X",  # type: ignore[arg-type]
        quality_threshold=0.99, max_rounds=2,
    )
    assert _Nation.critic_called is True
