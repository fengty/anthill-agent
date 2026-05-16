"""Plugin usage telemetry — accumulate so Scout can prefer what works.

Before v0.7.2 plugin calls were one-shot events: invoke web_search,
get a result, done. Whether web_search has historically worked for
this nation, whether shell has been failing recently, whether the
nation has even *used* pdf_read before — none of this was recorded
anywhere a decision-maker could read.

What this module does:

  1. PluginUsage — a single timestamped record per call
  2. append_plugin_usage / load_plugin_usage — jsonl I/O
  3. PluginStats / aggregate_usage — running success-rate per plugin
  4. format_plugin_stats_for_scout — a context block Scout can read
     when planning, so it leans toward plugins that have a track
     record over plugins it just heard of

The loop closes at the Scout side: when planning a new ask, the
formatter gives Scout a one-liner per plugin ("web_search: used 47
times, 91% success, avg 1.2s"). Scout's job is unchanged — it
decides whether/which plugin to invoke. But now it has the evidence.

Why this lives in core/ not plugins/: usage tracking is a nation
concern, not a plugin concern. A plugin shouldn't know about a
specific nation's history. The nation observes plugin behavior from
the outside.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PluginUsage:
    """One telemetry record. Cheap to write (4-6 fields) and easy to grep."""

    timestamp: float
    plugin_name: str
    ok: bool
    duration_seconds: float
    error: str | None = None
    output_size: int = 0  # length of str(output), useful for cost-ish proxies

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "plugin_name": self.plugin_name,
            "ok": self.ok,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "output_size": self.output_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PluginUsage":
        return cls(
            timestamp=float(data.get("timestamp", 0.0)),
            plugin_name=str(data.get("plugin_name", "")),
            ok=bool(data.get("ok", False)),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            error=data.get("error"),
            output_size=int(data.get("output_size", 0)),
        )


def usage_path(nation_dir: Path) -> Path:
    return nation_dir / "plugin_usage.jsonl"


def append_plugin_usage(record: PluginUsage, nation_dir: Path) -> None:
    nation_dir.mkdir(parents=True, exist_ok=True)
    with usage_path(nation_dir).open("a") as f:
        f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def load_plugin_usage(nation_dir: Path) -> list[PluginUsage]:
    path = usage_path(nation_dir)
    if not path.exists():
        return []
    out: list[PluginUsage] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            out.append(PluginUsage.from_dict(data))
    return out


# --- aggregation ----------------------------------------------------------


@dataclass
class PluginStats:
    """Running summary for one plugin."""

    plugin_name: str
    calls: int = 0
    successes: int = 0
    total_duration: float = 0.0
    last_used_at: float = 0.0
    top_error: str | None = None  # most common error message, if any
    _error_counts: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def success_rate(self) -> float:
        return self.successes / self.calls if self.calls else 0.0

    @property
    def avg_duration(self) -> float:
        return self.total_duration / self.calls if self.calls else 0.0


def aggregate_usage(records: list[PluginUsage]) -> dict[str, PluginStats]:
    """Walk every record once, produce per-plugin stats.

    Order-independent (commutative). Cheap enough to run on every
    Scout call — even 10k records process in < 50ms on a laptop.
    """
    stats: dict[str, PluginStats] = defaultdict(lambda: PluginStats(plugin_name=""))
    for r in records:
        if not r.plugin_name:
            continue
        s = stats[r.plugin_name]
        if not s.plugin_name:
            s.plugin_name = r.plugin_name
        s.calls += 1
        if r.ok:
            s.successes += 1
        else:
            err = (r.error or "unknown")[:80]
            s._error_counts[err] = s._error_counts.get(err, 0) + 1
        s.total_duration += r.duration_seconds
        if r.timestamp > s.last_used_at:
            s.last_used_at = r.timestamp
    # Backfill top_error
    for s in stats.values():
        if s._error_counts:
            s.top_error = max(s._error_counts.items(), key=lambda kv: kv[1])[0]
    return dict(stats)


def format_plugin_stats_for_scout(
    stats: dict[str, PluginStats],
    *,
    top_k: int = 6,
) -> str:
    """One-line-per-plugin block for Scout's planning context.

    Sorted by call count (the plugins the nation actually uses come
    first). Capped at top_k so the prompt doesn't bloat. Returns an
    empty string when there's nothing to share — Scout's prompt
    builder will skip it.
    """
    if not stats:
        return ""
    ordered = sorted(stats.values(), key=lambda s: -s.calls)[:top_k]
    lines = ["Plugin usage history for this nation:"]
    for s in ordered:
        rate_pct = s.success_rate * 100
        line = (
            f"  - {s.plugin_name}: used {s.calls} time(s), "
            f"{rate_pct:.0f}% success, avg {s.avg_duration:.2f}s"
        )
        if s.top_error and s.success_rate < 0.6:
            line += f' — common failure: "{s.top_error}"'
        lines.append(line)
    return "\n".join(lines)


# --- recording helpers ---------------------------------------------------


async def record_plugin_call(
    plugin,  # type: anthill.plugins.base.Plugin
    nation_dir: Path | None,
    **kwargs,
):  # noqa: ANN201
    """Call a plugin and persist a usage record.

    Returns the PluginResult unchanged so callers can use it normally.
    Persists to plugin_usage.jsonl iff `nation_dir` is given (CLI uses
    this; tests and direct programmatic use can pass None to skip I/O).

    Wraps any exception inside the plugin's call into an ok=False
    record — a misbehaving plugin should still leave a trace.
    """
    start = time.perf_counter()
    try:
        result = await plugin.call(**kwargs)
        duration = time.perf_counter() - start
        ok = bool(getattr(result, "ok", False))
        err = getattr(result, "error", None)
        out_size = len(str(getattr(result, "output", "")))
    except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
        duration = time.perf_counter() - start
        ok = False
        err = f"{type(exc).__name__}: {exc}"
        out_size = 0
        # Wrap into a PluginResult-ish shape so the caller still gets
        # a uniform return type. Imports localized to dodge cycles.
        from anthill.plugins.base import PluginResult
        result = PluginResult(output=None, ok=False, error=err)
    if nation_dir is not None:
        record = PluginUsage(
            timestamp=time.time(),
            plugin_name=plugin.name,
            ok=ok,
            duration_seconds=duration,
            error=err if not ok else None,
            output_size=out_size,
        )
        append_plugin_usage(record, nation_dir)
    return result


__all__ = [
    "PluginUsage",
    "PluginStats",
    "usage_path",
    "append_plugin_usage",
    "load_plugin_usage",
    "aggregate_usage",
    "format_plugin_stats_for_scout",
    "record_plugin_call",
]
