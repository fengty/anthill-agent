"""0.2.11 — auto-summarized wiki of what the user has been working on."""

from __future__ import annotations

import time
from pathlib import Path

from anthill.core.history import HistoryEntry
from anthill.core.wiki import (
    MIN_ASKS_FOR_TOPIC,
    Topic,
    build_topics,
    format_splash_line,
    format_wiki_markdown,
    refresh_wiki,
    wiki_path,
    write_wiki,
)


def _entry(
    request: str,
    *,
    ts: float = 0.0,
    ok: bool = True,
    id_: str = "x",
) -> HistoryEntry:
    return HistoryEntry(
        id=id_,
        timestamp=ts,
        request=request,
        plan=[],
        outcomes=[{"status": "ok" if ok else "failed"}],
    )


# --- build_topics --------------------------------------------------------


def test_build_topics_empty_history_returns_empty() -> None:
    assert build_topics([]) == []


def test_build_topics_below_threshold_dropped() -> None:
    """A single ask isn't a topic. MIN_ASKS_FOR_TOPIC is the gate."""
    history = [_entry("first ever ask", ts=100.0, id_="a")]
    topics = build_topics(history)
    assert topics == []


def test_build_topics_clusters_similar_asks() -> None:
    """Two asks sharing enough tokens become one topic."""
    history = [
        _entry("分析这个 mysql 中间件部署", ts=100.0, id_="a"),
        _entry("我要部署 mysql 中间件 怎么做", ts=200.0, id_="b"),
        _entry("写一首关于蚂蚁的诗", ts=300.0, id_="c"),
        _entry("关于蚂蚁 写一首诗 五言", ts=400.0, id_="d"),
    ]
    topics = build_topics(history, min_asks=MIN_ASKS_FOR_TOPIC)
    # Two clusters: mysql (2 asks) + 蚂蚁诗 (2 asks). Singles dropped.
    assert len(topics) == 2
    counts = {t.ask_count for t in topics}
    assert counts == {2}


def test_build_topics_orders_by_recency() -> None:
    """Most recently touched topic appears first (splash shows what
    user is doing NOW, not what they did a month ago)."""
    history = [
        _entry("mysql 中间件 1", ts=100.0, id_="a"),
        _entry("mysql 中间件 2", ts=200.0, id_="b"),
        _entry("飞书 对接 1", ts=300.0, id_="c"),
        _entry("飞书 对接 2", ts=400.0, id_="d"),
    ]
    topics = build_topics(history)
    assert len(topics) == 2
    # Lark cluster touched later (ts=400) → first.
    assert "飞书" in topics[0].name
    assert "mysql" in topics[1].name


def test_build_topics_topic_name_is_newest_phrasing() -> None:
    """When the user's wording evolves, the topic 'name' should be
    the latest version — not the oldest."""
    history = [
        _entry("mysql 中间件 怎么部署 早一些", ts=100.0, id_="a"),
        _entry("mysql 中间件 部署 标准方案 怎么 早一些", ts=200.0, id_="b"),
    ]
    topics = build_topics(history)
    assert len(topics) == 1
    # Latest wording wins.
    assert "标准方案" in topics[0].name


def test_build_topics_drops_failed_asks() -> None:
    """A topic the user TRIED but every attempt failed isn't a
    'topic they're working on' in any useful sense."""
    history = [
        _entry("doomed task 1", ts=100.0, id_="a", ok=False),
        _entry("doomed task 2", ts=200.0, id_="b", ok=False),
    ]
    assert build_topics(history) == []


# --- format_splash_line --------------------------------------------------


def test_splash_line_empty_for_no_topics() -> None:
    assert format_splash_line([]) == ""


def test_splash_line_includes_count() -> None:
    t = Topic(
        name="禅道 bug 分析",
        ask_count=5,
        last_touched=time.time(),
        entry_ids=("a", "b"),
        sample_requests=("禅道 bug 1", "禅道 bug 2"),
    )
    line = format_splash_line([t])
    assert "禅道 bug 分析" in line
    assert "(5×)" in line


def test_splash_line_truncates_long_names() -> None:
    long_name = "x" * 100
    t = Topic(
        name=long_name,
        ask_count=2,
        last_touched=time.time(),
        entry_ids=("a", "b"),
        sample_requests=(long_name,),
    )
    line = format_splash_line([t])
    # Name truncated to ~32 chars + ellipsis + count tail.
    assert len(line) < 60


def test_splash_line_respects_limit() -> None:
    topics = [
        Topic(
            name=f"topic-{i}",
            ask_count=2,
            last_touched=time.time() - i,
            entry_ids=("a",),
            sample_requests=(),
        )
        for i in range(10)
    ]
    line = format_splash_line(topics, limit=3)
    # Three topic names separated by ", ".
    assert line.count("topic-") == 3


# --- markdown rendering ------------------------------------------------


def test_markdown_empty_topics_explains() -> None:
    """An empty wiki still produces a readable file — don't write
    a confusing blank doc."""
    text = format_wiki_markdown([])
    assert "no topics yet" in text.lower() or "no topics yet." in text.lower() or "no topics yet" in text


def test_markdown_includes_topic_header() -> None:
    t = Topic(
        name="MySQL 部署",
        ask_count=3,
        last_touched=time.time() - 60,
        entry_ids=("a", "b", "c"),
        sample_requests=("MySQL 1", "MySQL 2", "MySQL 3"),
    )
    text = format_wiki_markdown([t])
    assert "# What this nation has been working on" in text
    assert "## MySQL 部署" in text
    assert "3 ask(s)" in text


def test_markdown_includes_humanized_ago() -> None:
    """Time-since-last-touched should be human-friendly Chinese."""
    t = Topic(
        name="Recent topic",
        ask_count=2,
        last_touched=time.time() - 120,  # 2 minutes ago
        entry_ids=("a", "b"),
        sample_requests=("ask 1", "ask 2"),
    )
    text = format_wiki_markdown([t])
    assert "分钟前" in text or "刚刚" in text


# --- I/O round trip ----------------------------------------------------


def test_write_wiki_creates_file(tmp_path: Path) -> None:
    t = Topic(
        name="test topic",
        ask_count=2,
        last_touched=time.time(),
        entry_ids=("a", "b"),
        sample_requests=("test 1", "test 2"),
    )
    write_wiki(tmp_path, [t])
    p = wiki_path(tmp_path)
    assert p.exists()
    assert "test topic" in p.read_text()


def test_write_wiki_handles_unwritable_dir_gracefully(tmp_path: Path) -> None:
    """Best-effort: even when the dir can't be created/written,
    write_wiki returns without raising."""
    # Point at a child of a file (not a dir) → mkdir will fail.
    file_in_the_way = tmp_path / "blocking"
    file_in_the_way.write_text("blocking")
    bad_dir = file_in_the_way / "subdir"
    # No exception expected.
    write_wiki(bad_dir, [])


# --- refresh_wiki end-to-end -------------------------------------------


def test_refresh_wiki_reads_history_writes_file(tmp_path: Path) -> None:
    """Drop a history.jsonl into tmp_path, call refresh_wiki, verify
    wiki.md appears and contains a topic."""
    import json

    (tmp_path / "history.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "id": f"id-{i}",
                    "timestamp": float(100 + i),
                    "request": f"mysql 部署 {i}",
                    "plan": [],
                    "outcomes": [{"status": "ok"}],
                }
            )
            for i in range(3)
        )
    )
    topics = refresh_wiki(tmp_path)
    assert len(topics) == 1  # 3 mysql asks → 1 topic
    # File on disk.
    assert wiki_path(tmp_path).exists()


def test_refresh_wiki_no_history_returns_empty(tmp_path: Path) -> None:
    """No history.jsonl yet (fresh nation) → no topics, no crash."""
    topics = refresh_wiki(tmp_path)
    assert topics == []
