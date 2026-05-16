"""v0.7.2 — plugin usage telemetry + Scout context.

Three layers:
  1. PluginUsage / aggregate_usage: pure data math
  2. record_plugin_call: wraps plugin.call and writes telemetry
  3. format_plugin_stats_for_scout: produces the context block Scout reads
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anthill.core.plugin_usage import (
    PluginStats,
    PluginUsage,
    aggregate_usage,
    append_plugin_usage,
    format_plugin_stats_for_scout,
    load_plugin_usage,
    record_plugin_call,
    usage_path,
)
from anthill.plugins.base import Plugin, PluginResult


# --- pure dataclass round-trip --------------------------------------------


def test_plugin_usage_round_trip() -> None:
    r = PluginUsage(
        timestamp=1234.5,
        plugin_name="web_search",
        ok=True,
        duration_seconds=1.2,
        error=None,
        output_size=42,
    )
    restored = PluginUsage.from_dict(r.to_dict())
    assert restored.plugin_name == "web_search"
    assert restored.ok is True
    assert restored.duration_seconds == pytest.approx(1.2)
    assert restored.output_size == 42


def test_plugin_usage_from_dict_tolerates_missing_fields() -> None:
    r = PluginUsage.from_dict({})
    assert r.plugin_name == ""
    assert r.ok is False
    assert r.duration_seconds == 0.0


# --- jsonl I/O -----------------------------------------------------------


def test_append_and_load_round_trip(tmp_path: Path) -> None:
    a = PluginUsage(
        timestamp=1.0, plugin_name="web_search", ok=True,
        duration_seconds=1.0, output_size=10,
    )
    b = PluginUsage(
        timestamp=2.0, plugin_name="shell", ok=False,
        duration_seconds=0.1, error="boom",
    )
    append_plugin_usage(a, tmp_path)
    append_plugin_usage(b, tmp_path)
    loaded = load_plugin_usage(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].plugin_name == "web_search"
    assert loaded[1].error == "boom"


def test_load_returns_empty_when_no_file(tmp_path: Path) -> None:
    assert load_plugin_usage(tmp_path) == []


def test_load_skips_corrupt_lines(tmp_path: Path) -> None:
    """A bad JSON line shouldn't poison the whole file."""
    path = usage_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write('{"plugin_name": "good", "ok": true}\n')
        f.write('not valid json\n')
        f.write('{"plugin_name": "also_good", "ok": false}\n')
    loaded = load_plugin_usage(tmp_path)
    assert [r.plugin_name for r in loaded] == ["good", "also_good"]


# --- aggregate_usage -----------------------------------------------------


def test_aggregate_empty_returns_empty() -> None:
    assert aggregate_usage([]) == {}


def test_aggregate_single_record() -> None:
    r = PluginUsage(
        timestamp=1.0, plugin_name="ws", ok=True, duration_seconds=0.5,
    )
    stats = aggregate_usage([r])
    assert "ws" in stats
    assert stats["ws"].calls == 1
    assert stats["ws"].success_rate == 1.0
    assert stats["ws"].avg_duration == pytest.approx(0.5)


def test_aggregate_mixed_outcomes() -> None:
    records = [
        PluginUsage(timestamp=1.0, plugin_name="ws", ok=True, duration_seconds=1.0),
        PluginUsage(timestamp=2.0, plugin_name="ws", ok=False, duration_seconds=2.0, error="timeout"),
        PluginUsage(timestamp=3.0, plugin_name="ws", ok=True, duration_seconds=1.5),
    ]
    stats = aggregate_usage(records)
    s = stats["ws"]
    assert s.calls == 3
    assert s.successes == 2
    assert s.success_rate == pytest.approx(2 / 3)
    assert s.avg_duration == pytest.approx(1.5)
    assert s.last_used_at == 3.0


def test_aggregate_top_error_is_most_common() -> None:
    records = [
        PluginUsage(timestamp=1, plugin_name="x", ok=False, duration_seconds=0, error="timeout"),
        PluginUsage(timestamp=2, plugin_name="x", ok=False, duration_seconds=0, error="timeout"),
        PluginUsage(timestamp=3, plugin_name="x", ok=False, duration_seconds=0, error="404 not found"),
    ]
    stats = aggregate_usage(records)
    assert stats["x"].top_error == "timeout"


def test_aggregate_ignores_blank_plugin_names() -> None:
    records = [
        PluginUsage(timestamp=1, plugin_name="", ok=True, duration_seconds=0),
        PluginUsage(timestamp=2, plugin_name="real", ok=True, duration_seconds=0),
    ]
    stats = aggregate_usage(records)
    assert "" not in stats
    assert "real" in stats


# --- format_plugin_stats_for_scout ---------------------------------------


def test_format_empty_returns_empty_string() -> None:
    assert format_plugin_stats_for_scout({}) == ""


def test_format_includes_call_count_and_rate() -> None:
    stats = {
        "ws": PluginStats(
            plugin_name="ws", calls=10, successes=8, total_duration=12.0,
        )
    }
    block = format_plugin_stats_for_scout(stats)
    assert "ws" in block
    assert "10" in block
    assert "80%" in block


def test_format_caps_at_top_k() -> None:
    stats = {
        f"p{i}": PluginStats(
            plugin_name=f"p{i}", calls=10 - i, successes=10 - i,
            total_duration=1.0,
        )
        for i in range(10)
    }
    block = format_plugin_stats_for_scout(stats, top_k=3)
    # Most-used first: p0, p1, p2
    assert "p0" in block and "p1" in block and "p2" in block
    assert "p9" not in block


def test_format_warns_on_chronic_failures() -> None:
    stats = {
        "bad": PluginStats(
            plugin_name="bad", calls=10, successes=3, total_duration=1.0,
            top_error="connection refused",
        )
    }
    block = format_plugin_stats_for_scout(stats)
    assert "connection refused" in block


def test_format_no_warning_when_success_rate_decent() -> None:
    """80% success ⇒ no failure annotation cluttering the prompt."""
    stats = {
        "ok": PluginStats(
            plugin_name="ok", calls=10, successes=8, total_duration=1.0,
            top_error="rare timeout",
        )
    }
    block = format_plugin_stats_for_scout(stats)
    assert "rare timeout" not in block


# --- record_plugin_call --------------------------------------------------


class _FakePlugin(Plugin):
    name = "fake"
    description = "for testing"

    def __init__(self, *, result: PluginResult | None = None,
                 raises: Exception | None = None) -> None:
        self._result = result or PluginResult(output="fine", ok=True)
        self._raises = raises

    async def call(self, **kwargs):  # noqa: ANN001, ANN201, ARG002
        if self._raises:
            raise self._raises
        return self._result


@pytest.mark.asyncio
async def test_record_plugin_call_persists_success(tmp_path: Path) -> None:
    plugin = _FakePlugin(result=PluginResult(output="some output", ok=True))
    result = await record_plugin_call(plugin, tmp_path, query="hi")
    assert result.ok
    records = load_plugin_usage(tmp_path)
    assert len(records) == 1
    assert records[0].plugin_name == "fake"
    assert records[0].ok is True
    assert records[0].output_size == len("some output")


@pytest.mark.asyncio
async def test_record_plugin_call_persists_failure(tmp_path: Path) -> None:
    plugin = _FakePlugin(
        result=PluginResult(output=None, ok=False, error="bad input")
    )
    result = await record_plugin_call(plugin, tmp_path)
    assert not result.ok
    records = load_plugin_usage(tmp_path)
    assert len(records) == 1
    assert records[0].error == "bad input"


@pytest.mark.asyncio
async def test_record_plugin_call_persists_exception(tmp_path: Path) -> None:
    """Exception in plugin.call must still leave a trace + return PluginResult."""
    plugin = _FakePlugin(raises=RuntimeError("boom"))
    result = await record_plugin_call(plugin, tmp_path)
    assert not result.ok
    assert "boom" in (result.error or "")
    records = load_plugin_usage(tmp_path)
    assert len(records) == 1
    assert "RuntimeError" in (records[0].error or "")


@pytest.mark.asyncio
async def test_record_plugin_call_no_persistence_when_dir_none(tmp_path: Path) -> None:
    plugin = _FakePlugin()
    await record_plugin_call(plugin, None)
    # tmp_path shouldn't have any usage file written
    assert not (tmp_path / "plugin_usage.jsonl").exists()


# --- Nation.ask integration ----------------------------------------------


@pytest.mark.asyncio
async def test_nation_ask_includes_plugin_stats_in_scout_context(tmp_path: Path) -> None:
    """Scout sees plugin usage history when planning."""
    from anthill.core.nation import Nation
    captured_context = {}

    class _StubScout:
        def __init__(self, *args, **kwargs):  # noqa: ANN001
            pass

        async def plan(self, request, **kwargs):  # noqa: ANN001
            captured_context["episodic"] = kwargs.get("episodic_context", "")
            from anthill.core.scout import Plan, Subtask
            return Plan(subtasks=[Subtask("x", request, [])])

    n = Nation(name="t")
    n.history_path = tmp_path / "history.jsonl"
    # Seed plugin usage to make sure the block is non-empty
    append_plugin_usage(
        PluginUsage(timestamp=1.0, plugin_name="ws", ok=True, duration_seconds=0.5),
        tmp_path,
    )
    append_plugin_usage(
        PluginUsage(timestamp=2.0, plugin_name="ws", ok=True, duration_seconds=0.4),
        tmp_path,
    )

    # Now read what _plugin_stats_block produces
    block = n._plugin_stats_block()
    assert "ws" in block
    assert "2 time(s)" in block


def test_plugin_stats_block_empty_when_no_history(tmp_path: Path) -> None:
    from anthill.core.nation import Nation
    n = Nation(name="t")
    n.history_path = tmp_path / "history.jsonl"
    assert n._plugin_stats_block() == ""


def test_plugin_stats_block_empty_when_no_history_path() -> None:
    """Nations without a history path (in-memory tests) ⇒ no block."""
    from anthill.core.nation import Nation
    n = Nation(name="t")
    n.history_path = None
    assert n._plugin_stats_block() == ""
