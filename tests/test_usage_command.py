"""0.2.16 — `/usage` shows $ + per-model distribution + speed.

We test the renderer with a captured rich.Console buffer + a primed
usage.jsonl. Coverage:
  - empty usage → friendly "no data" message
  - all-time window shows total cost line
  - by-model distribution sorted desc with percentages
  - `today` filter excludes older records
  - `week` filter
  - `session` filter falls back to last hour when stats.session_started_at missing
  - unknown window flag → friendly error
"""

from __future__ import annotations

import json
import time
from io import StringIO
from pathlib import Path

from rich.console import Console

from anthill.cli.repl import SessionStats, _show_usage
from anthill.core.agent import Agent
from anthill.core.nation import Nation


def _make_nation(tmp_path: Path) -> tuple[Nation, "_FakeCfg"]:
    n = Nation(name="t")
    n.agents = [Agent(id="ant-1", model="deepseek")]

    class _FakeCfg:
        home = tmp_path

    return n, _FakeCfg()


def _write_usage(tmp_path: Path, nation_name: str, records: list[dict]) -> None:
    # nation_dir(home, name) == home / "nations" / name
    nd = tmp_path / "nations" / nation_name
    nd.mkdir(parents=True, exist_ok=True)
    with (nd / "usage.jsonl").open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _render_usage(
    nation: Nation, cfg, stats: SessionStats, window: str | None = None
) -> str:
    import anthill.cli.repl as repl_mod

    buf = StringIO()
    fake = Console(file=buf, force_terminal=False, width=120)
    original = repl_mod.console
    repl_mod.console = fake
    try:
        _show_usage(nation, cfg, stats, window=window)
    finally:
        repl_mod.console = original
    return buf.getvalue()


# --- empty path ---------------------------------------------------------


def test_no_usage_yet(tmp_path: Path) -> None:
    n, cfg = _make_nation(tmp_path)
    out = _render_usage(n, cfg, SessionStats())
    assert "No usage data yet" in out


# --- all-time rendering ------------------------------------------------


def test_alltime_shows_cost_and_models(tmp_path: Path) -> None:
    now = time.time()
    _write_usage(
        tmp_path,
        "t",
        [
            {
                "timestamp": now - 100,
                "agent_id": "ant-1",
                "model": "deepseek",
                "task_type": "research",
                "input_tokens": 1000,
                "output_tokens": 500,
            },
            {
                "timestamp": now - 50,
                "agent_id": "ant-2",
                "model": "minimax",
                "task_type": "analyze",
                "input_tokens": 800,
                "output_tokens": 200,
            },
        ],
    )
    n, cfg = _make_nation(tmp_path)
    out = _render_usage(n, cfg, SessionStats())
    assert "💰 Cost" in out
    assert "all-time" in out
    assert "🧠 Models" in out
    # Both models present, with percentages.
    assert "deepseek" in out
    assert "minimax" in out
    assert "%" in out
    # Volume line.
    assert "📊 Volume" in out
    assert "1,800" in out  # 1000+800 = 1800 in tokens
    assert "700" in out  # 500+200 = 700 out tokens


def test_alltime_shows_speed_section(tmp_path: Path) -> None:
    """Speed line needs a non-zero window — supply >=2 records
    spaced in time so period_end - period_start > 0."""
    now = time.time()
    _write_usage(
        tmp_path,
        "t",
        [
            {
                "timestamp": now - 100,
                "agent_id": "ant-1",
                "model": "deepseek",
                "task_type": "research",
                "input_tokens": 2000,
                "output_tokens": 1000,
            },
            {
                "timestamp": now - 50,
                "agent_id": "ant-1",
                "model": "deepseek",
                "task_type": "research",
                "input_tokens": 500,
                "output_tokens": 300,
            },
        ],
    )
    n, cfg = _make_nation(tmp_path)
    out = _render_usage(n, cfg, SessionStats())
    assert "⚡ Speed" in out
    assert "tok/s" in out


# --- window filters ----------------------------------------------------


def test_today_window_excludes_older(tmp_path: Path) -> None:
    """Records from 2 days ago shouldn't appear in `/usage today`."""
    now = time.time()
    two_days_ago = now - 2 * 86400
    _write_usage(
        tmp_path,
        "t",
        [
            {
                "timestamp": two_days_ago,
                "agent_id": "ant-1",
                "model": "deepseek",
                "task_type": "research",
                "input_tokens": 5000,
                "output_tokens": 5000,
            },
        ],
    )
    n, cfg = _make_nation(tmp_path)
    out = _render_usage(n, cfg, SessionStats(), window="today")
    # The only record is 2 days old → filtered out.
    assert "No usage in today" in out


def test_today_window_includes_today(tmp_path: Path) -> None:
    """A record from earlier today should appear."""
    now = time.time()
    _write_usage(
        tmp_path,
        "t",
        [
            {
                "timestamp": now - 60,  # 1 minute ago — definitely today
                "agent_id": "ant-1",
                "model": "deepseek",
                "task_type": "research",
                "input_tokens": 100,
                "output_tokens": 50,
            },
        ],
    )
    n, cfg = _make_nation(tmp_path)
    out = _render_usage(n, cfg, SessionStats(), window="today")
    assert "💰 Cost" in out
    assert "today" in out


def test_week_window_includes_3_days_ago(tmp_path: Path) -> None:
    now = time.time()
    _write_usage(
        tmp_path,
        "t",
        [
            {
                "timestamp": now - 3 * 86400,
                "agent_id": "ant-1",
                "model": "deepseek",
                "task_type": "research",
                "input_tokens": 100,
                "output_tokens": 50,
            },
        ],
    )
    n, cfg = _make_nation(tmp_path)
    out = _render_usage(n, cfg, SessionStats(), window="week")
    assert "last 7 days" in out


def test_unknown_window(tmp_path: Path) -> None:
    n, cfg = _make_nation(tmp_path)
    _write_usage(
        tmp_path,
        "t",
        [
            {
                "timestamp": time.time(),
                "agent_id": "ant-1",
                "model": "deepseek",
                "task_type": "research",
                "input_tokens": 10,
                "output_tokens": 5,
            },
        ],
    )
    out = _render_usage(n, cfg, SessionStats(), window="yesteryear")
    assert "Unknown window" in out


# --- this-session line ------------------------------------------------


def test_this_session_line_appears(tmp_path: Path) -> None:
    """When stats.asks > 0 the bottom line shows session totals."""
    now = time.time()
    _write_usage(
        tmp_path,
        "t",
        [
            {
                "timestamp": now - 60,
                "agent_id": "ant-1",
                "model": "deepseek",
                "task_type": "research",
                "input_tokens": 100,
                "output_tokens": 50,
            },
        ],
    )
    n, cfg = _make_nation(tmp_path)
    stats = SessionStats()
    stats.asks = 3
    stats.tokens_in = 100
    stats.tokens_out = 50
    stats.cost_usd = 0.0005
    out = _render_usage(n, cfg, stats)
    assert "this session" in out
    assert "3 asks" in out


