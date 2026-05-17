"""0.1.17 — skill auto-mining tests.

Detects clusters of similar past asks so the REPL can nudge "you've
done this N times — save as a recipe?" Quiet otherwise.
"""

from __future__ import annotations

import time


def _entry(req: str, *, succeeded: bool = True, ts: float | None = None, eid: str | None = None):
    """Tiny HistoryEntry factory with the only fields the miner reads."""
    from anthill.core.history import HistoryEntry

    ts = ts if ts is not None else time.time()
    outcomes = [{"status": "ok"}] if succeeded else [{"status": "failed"}]
    return HistoryEntry(
        id=eid or HistoryEntry.make_id(req, ts),
        timestamp=ts,
        request=req,
        plan=[],
        outcomes=outcomes,
    )


def test_no_recurring_returns_empty() -> None:
    from anthill.core.skill_mining import mine_skills

    history = [
        _entry("translate this to French"),
        _entry("what is the weather"),
        _entry("explain stigmergy"),
    ]
    assert mine_skills(history) == []


def test_three_similar_form_a_cluster() -> None:
    from anthill.core.skill_mining import mine_skills

    history = [
        _entry("translate this to French and explain choices"),
        _entry("translate this to French and explain the wording"),
        _entry("translate this to French — explain choices"),
    ]
    skills = mine_skills(history, min_occurrences=3)
    assert len(skills) == 1
    assert skills[0].occurrences == 3


def test_failed_asks_dont_count() -> None:
    """Repeating a failing query is not a skill — only ok outcomes contribute."""
    from anthill.core.skill_mining import mine_skills

    history = [
        _entry("draft a press release", succeeded=False),
        _entry("draft a press release", succeeded=False),
        _entry("draft a press release", succeeded=False),
    ]
    assert mine_skills(history, min_occurrences=3) == []


def test_min_occurrences_threshold_respected() -> None:
    from anthill.core.skill_mining import mine_skills

    history = [
        _entry("research the X protocol"),
        _entry("research the X protocol again"),
    ]
    # min=3 ⇒ this cluster of 2 doesn't surface
    assert mine_skills(history, min_occurrences=3) == []
    # min=2 ⇒ it does
    skills = mine_skills(history, min_occurrences=2)
    assert len(skills) == 1


def test_representative_is_most_recent() -> None:
    """The 'name what the user is about to type' picks the freshest phrasing."""
    from anthill.core.skill_mining import mine_skills

    base = time.time()
    history = [
        _entry("translate this to French", ts=base + 0),
        _entry("translate this to French and explain", ts=base + 10),
        _entry("translate this to French with notes", ts=base + 20),
    ]
    skills = mine_skills(history, min_occurrences=3)
    assert skills[0].representative == "translate this to French with notes"


def test_clusters_ordered_by_occurrence_desc() -> None:
    from anthill.core.skill_mining import mine_skills

    history = (
        [_entry("translate this to French", ts=i) for i in range(1, 6)]  # 5×
        + [_entry("research the X protocol", ts=i) for i in range(10, 13)]  # 3×
    )
    skills = mine_skills(history, min_occurrences=3)
    assert len(skills) == 2
    assert skills[0].occurrences == 5
    assert skills[1].occurrences == 3


def test_looks_like_new_match() -> None:
    from anthill.core.skill_mining import (
        DEFAULT_MIN_OCCURRENCES,
        MinedSkill,
        looks_like_new_match,
    )

    s = MinedSkill(
        representative="translate this to French and explain choices",
        occurrences=DEFAULT_MIN_OCCURRENCES,
        entry_ids=("a", "b", "c"),
        latest_timestamp=time.time(),
    )
    assert looks_like_new_match(s, "translate this to French and explain")
    assert not looks_like_new_match(s, "what is the weather today")


def test_empty_request_doesnt_seed_cluster() -> None:
    """A blank request would tokenize to empty and shouldn't anchor a cluster."""
    from anthill.core.skill_mining import mine_skills

    history = [_entry("") for _ in range(5)]
    assert mine_skills(history, min_occurrences=3) == []


def test_scan_limit_caps_inspection() -> None:
    """When history is huge, only the recent slice is examined."""
    from anthill.core.skill_mining import mine_skills

    base = time.time()
    # 5 old matches, then 200 unrelated recent entries. With
    # scan_limit=50 we should miss the cluster.
    history = (
        [_entry("translate to French", ts=base + i, eid=f"old{i}") for i in range(5)]
        + [_entry(f"unique question {i}", ts=base + 100 + i, eid=f"new{i}") for i in range(200)]
    )
    skills = mine_skills(history, min_occurrences=3, scan_limit=50)
    # The "translate to French" cluster got trimmed off — nothing surfaces.
    assert all("translate to French" not in s.representative for s in skills)


def test_freshness_window_days() -> None:
    from anthill.core.skill_mining import MinedSkill, freshness_window_days

    s = MinedSkill(
        representative="x",
        occurrences=3,
        entry_ids=("a",),
        latest_timestamp=time.time() - 86400 * 2,  # 2 days ago
    )
    days = freshness_window_days(s)
    assert 1.9 <= days <= 2.1
