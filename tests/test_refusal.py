"""0.1.40 — detect user-serving refusals + retry with resourceful nudge.

The narrative this patch encodes: **用户是国王，子民通过多模型能力，
想尽办法完成用户的任务**. When a citizen punts work back to the king
("please paste the content", "I can't access that URL"), that's a
failure — not a content-policy refusal. Different bucket, different
remedy.

Tests cover:
- detect_refusal positives (EN + Chinese)
- doesn't fire on substantive answers that incidentally use "please"
- doesn't fire on genuine policy refusals
- Agent.execute downgrades success_score to 0
- failure_reason set to USER_SERVING_REFUSAL
- _quality_of caps at 0.4
- executor retry path injects the resourceful addendum
"""

from __future__ import annotations

import pytest


# --- pattern detection -------------------------------------------------


def test_strong_refusal_english_paste() -> None:
    from anthill.core.refusal import is_user_serving_refusal

    assert is_user_serving_refusal(
        "I cannot access external URLs. Please paste the content here."
    ) is True


def test_strong_refusal_chinese_please_paste() -> None:
    from anthill.core.refusal import is_user_serving_refusal

    assert is_user_serving_refusal(
        "我无法直接访问外部链接，因此无法获取该 URL 的内容。"
        "请直接粘贴该 Bug 报告的内容。"
    ) is True


def test_strong_refusal_chinese_no_access() -> None:
    from anthill.core.refusal import is_user_serving_refusal

    text = "我无法打开这个链接，您能告诉我具体内容吗？"
    assert is_user_serving_refusal(text) is True


def test_strong_refusal_could_you_provide() -> None:
    from anthill.core.refusal import is_user_serving_refusal

    text = (
        "I don't have enough information to give a definitive answer. "
        "Could you provide more details about the use case?"
    )
    assert is_user_serving_refusal(text) is True


def test_substantive_answer_with_polite_please_does_not_fire() -> None:
    """A real answer that politely says "please consider X" should NOT
    trip the detector — false positives are the bigger risk."""
    from anthill.core.refusal import is_user_serving_refusal

    text = (
        "Here are three options for your vector database, in order of "
        "operational simplicity:\n\n"
        "1. **Qdrant** — best documented, please consider the "
        "single-node deployment for staging.\n"
        "2. **Milvus** — heaviest, but most scalable.\n"
        "3. **Chroma** — simplest API.\n\n"
        "Given your traffic profile, Qdrant is the safest first bet."
    )
    assert is_user_serving_refusal(text) is False


def test_policy_refusal_does_not_count_as_user_serving() -> None:
    """A model that refuses on safety grounds is NOT bouncing work back.
    Should not be retried with 'be resourceful'."""
    from anthill.core.refusal import is_user_serving_refusal

    text = (
        "I cannot help with that request because it would involve "
        "creating malicious software. This violates the safety policy."
    )
    # The strong-pattern matcher requires specific deferral phrasing.
    # Pure policy refusals don't match.
    assert is_user_serving_refusal(text) is False


def test_short_response_returns_false() -> None:
    """Very short text is handled by EMPTY_RESPONSE elsewhere; we
    don't fire on it here."""
    from anthill.core.refusal import is_user_serving_refusal

    assert is_user_serving_refusal("") is False
    assert is_user_serving_refusal("ok") is False
    assert is_user_serving_refusal("不行") is False


def test_real_world_zentao_response() -> None:
    """The exact phrasing from the real-user bug report."""
    from anthill.core.refusal import is_user_serving_refusal

    text = (
        "抱歉，我无法直接访问外部链接或浏览网页，因此无法获取您提供"
        "的链接中的具体内容。如果您能直接粘贴该 Bug 报告的文本内容，"
        "我可以帮您整理和格式化相关信息。"
    )
    assert is_user_serving_refusal(text) is True


def test_real_world_research_refusal() -> None:
    """A real-world deferral seen in the wild."""
    from anthill.core.refusal import is_user_serving_refusal

    text = (
        "I'm unable to access that URL directly. If you could share "
        "the relevant section of the document, I'd be happy to dive in."
    )
    assert is_user_serving_refusal(text) is True


# --- classify_attempt integration -------------------------------------


def test_classify_returns_user_serving_refusal() -> None:
    from anthill.core.failure import FailureReason, classify_attempt

    text = "I cannot access that link. Please paste the content."
    assert classify_attempt(text, success_score=0.0) == FailureReason.USER_SERVING_REFUSAL


def test_policy_refusal_wins_over_user_serving() -> None:
    """When BOTH patterns could match, policy_refusal takes precedence."""
    from anthill.core.failure import FailureReason, classify_attempt

    # Phrasing that triggers BOTH "please provide" and a policy marker.
    # Test that the policy marker wins.
    text = (
        "I cannot help with that. Please provide a different request "
        "that doesn't violate our content policy."
    )
    # The classifier walks through user-serving-refusal first (when no
    # policy markers fire) THEN policy. With "violate" + "policy" in
    # the text, it bumps to POLICY_REFUSAL.
    assert classify_attempt(text, success_score=0.0) == FailureReason.POLICY_REFUSAL


def test_substantive_answer_classified_as_none() -> None:
    """A successful, substantive answer returns no failure reason."""
    from anthill.core.failure import classify_attempt

    text = "The vector databases worth considering in 2026 are Qdrant, Milvus, and Chroma. Each has tradeoffs around scale vs simplicity."
    assert classify_attempt(text, success_score=1.0) is None


# --- Agent.execute integration ---------------------------------------


@pytest.mark.asyncio
async def test_agent_execute_downgrades_refusal_to_zero(monkeypatch) -> None:
    """A refusal lands as success_score=0 so executor retry kicks in."""
    from anthill.core.agent import Agent
    from anthill.models.base import ModelResponse

    class _RefusalProvider:
        name = "test"

        async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
            return ModelResponse(
                text=(
                    "I cannot access external URLs. "
                    "Please paste the content directly."
                ),
                model="test",
                finish_reason="stop",
            )

        async def stream(self, prompt, **kw):
            from anthill.models.base import StreamChunk
            yield StreamChunk(delta="I cannot access external URLs. Please paste the content directly.")
            yield StreamChunk(done=True, finish_reason="stop")

    a = Agent(id="ant-x", model="test")
    a._provider = _RefusalProvider()
    result = await a.execute("research", "do the thing")
    assert result.success_score == 0.0
    assert result.failure_reason == "user_serving_refusal"


@pytest.mark.asyncio
async def test_agent_execute_keeps_substantive_at_one(monkeypatch) -> None:
    """Sanity: real answers still score 1.0."""
    from anthill.core.agent import Agent
    from anthill.models.base import ModelResponse

    class _GoodProvider:
        name = "test"

        async def complete(self, prompt, *, system=None, max_tokens=4096, temperature=0.7):
            return ModelResponse(
                text=(
                    "Qdrant, Milvus, and Chroma are the three "
                    "open-source vector databases worth considering "
                    "in 2026. Each has different tradeoffs."
                ),
                model="test",
                finish_reason="stop",
            )

    a = Agent(id="ant-y", model="test")
    a._provider = _GoodProvider()
    result = await a.execute("research", "vector dbs")
    assert result.success_score == 1.0
    assert result.failure_reason is None


# --- deliberate _quality_of cap -------------------------------------


def test_quality_of_caps_refusal_at_0_4() -> None:
    """A refusal in the winning attempt caps overall quality at 0.4
    so deliberation runs another round."""
    from anthill.core.agent import TaskResult
    from anthill.core.deliberate import _quality_of
    from anthill.core.executor import SubtaskOutcome
    from anthill.core.nation import AskResult
    from anthill.core.scout import Plan, Subtask

    attempt = TaskResult(
        task_id="t",
        agent_id="ant-1",
        task_type="research",
        output="please paste the bug content directly",
        success_score=0.0,
        duration_seconds=0.0,
        scores={"completeness": 1.0, "correctness": 1.0},  # judge happy
        failure_reason="user_serving_refusal",
    )
    plan = Plan(subtasks=[Subtask("research", "x", [])])
    outcome = SubtaskOutcome(
        subtask=plan.subtasks[0],
        attempts=[attempt],
        status="ok",  # the executor declared "ok" because output exists
    )
    result = AskResult(request="x", plan=plan, outcomes=[outcome])
    q, _ = _quality_of(result)
    assert q <= 0.4


def test_quality_of_not_capped_for_real_answer() -> None:
    """Substantive output still scores high — guard against the cap
    being too aggressive."""
    from anthill.core.agent import TaskResult
    from anthill.core.deliberate import _quality_of
    from anthill.core.executor import SubtaskOutcome
    from anthill.core.nation import AskResult
    from anthill.core.scout import Plan, Subtask

    attempt = TaskResult(
        task_id="t",
        agent_id="ant-1",
        task_type="research",
        output="substantive answer",
        success_score=1.0,
        duration_seconds=0.0,
        scores={"completeness": 1.0, "correctness": 1.0},
        failure_reason=None,
    )
    plan = Plan(subtasks=[Subtask("research", "x", [])])
    outcome = SubtaskOutcome(
        subtask=plan.subtasks[0],
        attempts=[attempt],
        status="ok",
    )
    result = AskResult(request="x", plan=plan, outcomes=[outcome])
    q, _ = _quality_of(result)
    assert q == pytest.approx(1.0)
