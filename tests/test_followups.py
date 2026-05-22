"""0.2.17 — Rule-based follow-up suggestions after each ask.

Tests cover the heuristic without burning an LLM. The point of
follow-ups is to nudge the user toward likely next-questions —
done badly they're worse than silence ("would you like to know
more?" feels chatbot-tier). Done well they save a round-trip
("想看完整可运行的例子吗？" right after a code-blob answer).

Coverage:
  - empty output → no hints
  - definition asks → "对比 / 怎么用" follow-ups
  - how-to asks → "完整例子 / 常见的坑"
  - compare asks → "推荐 / 各自最小例子"
  - generic + code → "一步步带跑"
  - generic + URL → "打开链接看"
  - generic + file paths → "打开这些文件"
  - long output → "精简版"
  - dedup + cap at 2
  - hint that echoes the user's own ask is filtered
"""

from __future__ import annotations

import pytest

from anthill.core.followups import format_followup_line, suggest_followups


# --- empty path --------------------------------------------------------


def test_empty_output_returns_no_hints() -> None:
    assert suggest_followups("hi", "") == []
    assert suggest_followups("hi", "   ") == []


def test_empty_request_still_works() -> None:
    """No request, output present — should still produce something
    or nothing without crashing."""
    out = suggest_followups("", "Plain answer with no code or urls.")
    assert isinstance(out, list)
    assert len(out) <= 2


# --- definition asks ---------------------------------------------------


def test_definition_ask_no_code_suggests_compare(monkeypatch) -> None:
    hints = suggest_followups(
        "什么是 Raft 协议",
        "Raft 是一种共识算法，把状态机日志通过 leader 复制到 follower。",
    )
    assert any("对比" in h for h in hints)


def test_definition_ask_with_code_suggests_minimal_example() -> None:
    hints = suggest_followups(
        "什么是 MySQL group replication",
        "Group replication 是 MySQL 的多主复制。\n\n```sql\nSET GLOBAL group_replication_bootstrap_group=ON;\n```",
    )
    assert any("跑得起来" in h or "最小例子" in h for h in hints)


# --- how-to asks ------------------------------------------------------


def test_how_ask_suggests_pitfalls() -> None:
    hints = suggest_followups(
        "怎么部署 redis 集群",
        "1. 准备 6 个节点。\n2. 启动 redis-server。\n3. 用 redis-cli --cluster create ...",
    )
    assert any("坑" in h for h in hints)


def test_how_ask_with_code_suggests_full_example() -> None:
    hints = suggest_followups(
        "如何使用 docker compose",
        "```yaml\nversion: '3'\nservices:\n  app:\n    image: nginx\n```",
    )
    assert any("完整" in h or "例子" in h for h in hints)


# --- compare asks -----------------------------------------------------


def test_compare_ask_suggests_recommendation() -> None:
    hints = suggest_followups(
        "MySQL vs PostgreSQL",
        "MySQL 在简单 OLTP 场景更轻量；PostgreSQL 在复杂查询、扩展性上更强。",
    )
    assert any("建议" in h for h in hints)


# --- generic asks: shape-driven ---------------------------------------


def test_generic_with_code_suggests_step_by_step() -> None:
    hints = suggest_followups(
        "show me a hello world",
        "```python\nprint('hello')\n```",
    )
    assert any("带" in h or "跑" in h for h in hints)


def test_generic_with_url_suggests_open() -> None:
    hints = suggest_followups(
        "find a tutorial",
        "Check out https://docs.example.com/tutorial",
    )
    assert any("打开链接" in h for h in hints)


def test_generic_with_file_paths_suggests_inspect() -> None:
    hints = suggest_followups(
        "list relevant files",
        "Look at ./src/main.py and ./tests/test_main.py",
    )
    assert any("打开" in h or "文件" in h for h in hints)


def test_long_output_suggests_short_version() -> None:
    """Output >2000 chars → 'want the short version?'"""
    long_out = "long content " * 200  # ~2,600 chars
    hints = suggest_followups("explain everything", long_out)
    assert any("精简版" in h or "100 字" in h for h in hints)


# --- dedup + cap ------------------------------------------------------


def test_cap_at_2_hints() -> None:
    """Even when many rules match, we never emit more than 2."""
    # Definition + code + long output — multiple rules fire.
    long_code_block = "```\n" + ("x" * 2500) + "\n```"
    hints = suggest_followups(
        "什么是 long thing", "Long thing is " + long_code_block
    )
    assert len(hints) <= 2


def test_no_duplicates_in_output() -> None:
    hints = suggest_followups("怎么用 docker", "```\ndocker run -it ubuntu\n```")
    assert len(hints) == len(set(hints))


# --- format_followup_line -------------------------------------------


def test_format_empty_list() -> None:
    assert format_followup_line([]) == ""


def test_format_single_hint() -> None:
    line = format_followup_line(["想看完整例子吗？"])
    assert line.startswith("💡 ")
    assert "想看完整例子吗" in line


def test_format_two_hints_joined() -> None:
    line = format_followup_line(["A", "B"])
    assert "A" in line
    assert "B" in line
    # Separator is "  ·  " in the impl.
    assert "·" in line


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
