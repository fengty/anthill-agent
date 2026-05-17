"""v0.8.2 — multi-model collaboration visualization.

The UI itself is bash/rich output, hard to unit-test directly. But the
*data flow* it relies on can be verified: each subtask outcome carries
its winning citizen's agent_id, and we should be able to look up the
model from nation.agents.
"""

from __future__ import annotations

from anthill.core.agent import Agent, TaskResult
from anthill.core.executor import SubtaskOutcome
from anthill.core.scout import Subtask


# --- Data plumbing the collaboration card depends on ---------------------


def test_outcome_final_agent_id_available() -> None:
    """Outcome.final.agent_id is the data point the viz reads.

    If this breaks, the collaboration card silently degrades to '?'.
    Pin the contract so we notice.
    """
    attempt = TaskResult(
        task_id="t", agent_id="ant-deepseek-1", task_type="research",
        output="findings", success_score=1.0, duration_seconds=0.0,
    )
    outcome = SubtaskOutcome(
        subtask=Subtask("research", "dig", []),
        attempts=[attempt],
        status="ok",
    )
    assert outcome.final is attempt
    assert outcome.final.agent_id == "ant-deepseek-1"


def test_nation_agents_carry_model_field() -> None:
    """The viz looks up model via `for a in nation.agents: if a.id == ...`.

    Pin that the model attribute exists with whatever value the user
    configured at spawn time.
    """
    a = Agent(id="ant-x", model="minimax")
    assert a.model == "minimax"


def test_distinct_models_can_co_exist_in_one_nation() -> None:
    """v0.8.2 headline is "K models collaborated" — only meaningful if
    a nation can actually carry citizens on different models."""
    from anthill.core.nation import Nation
    n = Nation(name="t")
    n.agents = [
        Agent(id="ant-d1", model="deepseek-chat"),
        Agent(id="ant-d2", model="deepseek-chat"),
        Agent(id="ant-m1", model="minimax"),
        Agent(id="ant-g1", model="gpt-4"),
    ]
    models = {a.model for a in n.agents}
    assert len(models) == 3


def test_collaboration_data_extractable_from_askresult() -> None:
    """End-to-end shape: AskResult.outcomes → (task_type, agent_id, model)."""
    from anthill.core.nation import AskResult
    from anthill.core.scout import Plan

    plan = Plan(subtasks=[
        Subtask("research", "p1", []),
        Subtask("summarize", "p2", ["research"]),
    ])

    results = [
        TaskResult(task_id="t1", agent_id="ant-deep-1", task_type="research",
                   output="dat", success_score=1.0, duration_seconds=0.0),
        TaskResult(task_id="t2", agent_id="ant-mini-1", task_type="summarize",
                   output="brief", success_score=1.0, duration_seconds=0.0),
    ]
    outcomes = [
        SubtaskOutcome(subtask=plan.subtasks[0], attempts=[results[0]], status="ok"),
        SubtaskOutcome(subtask=plan.subtasks[1], attempts=[results[1]], status="ok"),
    ]

    ar = AskResult(request="r", plan=plan, outcomes=outcomes)
    # Extract collaboration tuples the way the REPL renderer does
    collab = [
        (o.subtask.task_type, o.final.agent_id)
        for o in ar.outcomes
        if o.status == "ok" and o.final is not None
    ]
    assert collab == [("research", "ant-deep-1"), ("summarize", "ant-mini-1")]


def test_skipped_subtasks_excluded_from_collaboration_view() -> None:
    """Skipped (dep failed / budget) outcomes have no `final` — viz must skip."""
    plan_sub = Subtask("late", "p", [])
    skipped = SubtaskOutcome(
        subtask=plan_sub, attempts=[], status="skipped",
        skip_reason="dependency 'research' failed",
    )
    assert skipped.final is None
    # Viz filter: `if o.status == "ok" and o.final is not None`
    assert not (skipped.status == "ok" and skipped.final is not None)


def test_model_count_reflects_actual_diversity() -> None:
    """Headline `K models collaborated` should count distinct models, not citizens."""
    # 3 citizens but only 2 distinct models — header should say "2"
    citizens = [
        ("ant-a", "deepseek-chat"),
        ("ant-b", "deepseek-chat"),
        ("ant-c", "minimax"),
    ]
    # Same dedup logic the renderer uses:
    distinct_models = {model for _, model in citizens}
    assert len(distinct_models) == 2
