"""v0.7 — history hash chain integrity.

The contract:
  - append_history populates prev_hash / chain_hash / chain_version
  - prev_hash chains to the previous entry's chain_hash (genesis for
    the first one)
  - Recomputing the hash of an entry's content reproduces chain_hash
  - verify_chain catches tampering at any position
  - Legacy entries (no chain fields) are reported but not flagged as
    broken — the hash chain protects everything from v0.7 forward
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anthill.core.history import (
    CHAIN_VERSION,
    GENESIS_PREV_HASH,
    HistoryEntry,
    _entry_hash,
    append_history,
    history_path,
    load_history,
    verify_chain,
)


def _make_entry(idx: int) -> HistoryEntry:
    """Construct a deterministic test entry."""
    return HistoryEntry(
        id=f"id{idx:05d}",
        timestamp=1000.0 + idx,
        request=f"request {idx}",
        plan=[{"task_type": "x", "depends_on": []}],
        outcomes=[{"task_type": "x", "status": "ok", "attempts": 1}],
    )


# --- append_history wires chain fields ------------------------------------


def test_first_entry_has_genesis_prev_hash(tmp_path: Path) -> None:
    append_history(_make_entry(0), tmp_path)
    entries = load_history(tmp_path)
    assert entries[0].prev_hash == GENESIS_PREV_HASH
    assert entries[0].chain_hash != ""
    assert entries[0].chain_version == CHAIN_VERSION


def test_subsequent_entry_prev_hash_chains_to_previous(tmp_path: Path) -> None:
    append_history(_make_entry(0), tmp_path)
    append_history(_make_entry(1), tmp_path)
    entries = load_history(tmp_path)
    assert entries[1].prev_hash == entries[0].chain_hash


def test_recomputed_hash_matches_stored(tmp_path: Path) -> None:
    append_history(_make_entry(0), tmp_path)
    entries = load_history(tmp_path)
    assert _entry_hash(entries[0]) == entries[0].chain_hash


def test_chain_hash_changes_if_content_changes() -> None:
    """Two entries with different content must produce different chain_hash."""
    a = HistoryEntry(
        id="x", timestamp=1.0, request="hello", plan=[], outcomes=[],
        prev_hash=GENESIS_PREV_HASH,
    )
    b = HistoryEntry(
        id="x", timestamp=1.0, request="hello world", plan=[], outcomes=[],
        prev_hash=GENESIS_PREV_HASH,
    )
    assert _entry_hash(a) != _entry_hash(b)


# --- verify_chain — clean cases -------------------------------------------


def test_verify_empty_history(tmp_path: Path) -> None:
    status = verify_chain(tmp_path)
    assert status.total == 0
    assert status.ok


def test_verify_single_entry_clean(tmp_path: Path) -> None:
    append_history(_make_entry(0), tmp_path)
    status = verify_chain(tmp_path)
    assert status.ok
    assert status.chained_count == 1
    assert status.legacy_count == 0


def test_verify_many_entries_clean(tmp_path: Path) -> None:
    for i in range(20):
        append_history(_make_entry(i), tmp_path)
    status = verify_chain(tmp_path)
    assert status.ok
    assert status.chained_count == 20


# --- verify_chain — tampering detection -----------------------------------


def test_verify_detects_request_tampering(tmp_path: Path) -> None:
    """Editing the request text after-the-fact must trip the hash check."""
    for i in range(5):
        append_history(_make_entry(i), tmp_path)
    path = history_path(tmp_path)
    lines = path.read_text().splitlines()
    # Tamper with entry #2's request without recomputing chain_hash.
    record = json.loads(lines[2])
    record["request"] = "MUTATED"
    lines[2] = json.dumps(record, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    status = verify_chain(tmp_path)
    assert not status.ok
    assert status.broken_at_index == 2
    assert "recomputed hash" in (status.broken_reason or "")


def test_verify_detects_outcome_tampering(tmp_path: Path) -> None:
    """A subtler edit (flipping an outcome status) still breaks the chain."""
    for i in range(3):
        append_history(_make_entry(i), tmp_path)
    path = history_path(tmp_path)
    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["outcomes"][0]["status"] = "failed"
    lines[1] = json.dumps(record, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    status = verify_chain(tmp_path)
    assert not status.ok
    assert status.broken_at_index == 1


def test_verify_detects_prev_hash_tampering(tmp_path: Path) -> None:
    """Even pointing prev_hash at a wrong entry should break the chain."""
    for i in range(3):
        append_history(_make_entry(i), tmp_path)
    path = history_path(tmp_path)
    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["prev_hash"] = "0" * 64  # genesis — but this isn't position 0
    lines[1] = json.dumps(record, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    status = verify_chain(tmp_path)
    assert not status.ok
    assert status.broken_at_index == 1


def test_verify_detects_entry_deletion(tmp_path: Path) -> None:
    """If someone removes a middle entry, the next prev_hash no longer matches."""
    for i in range(4):
        append_history(_make_entry(i), tmp_path)
    path = history_path(tmp_path)
    lines = path.read_text().splitlines()
    # Drop entry #1; entry #2's prev_hash should now point at the
    # now-missing #1's chain_hash, but the verifier expects #0's.
    new_lines = [lines[0]] + lines[2:]
    path.write_text("\n".join(new_lines) + "\n")

    status = verify_chain(tmp_path)
    assert not status.ok
    assert status.broken_at_index == 1


# --- legacy compatibility -------------------------------------------------


def test_verify_legacy_entries_without_chain_fields(tmp_path: Path) -> None:
    """Pre-v0.7 entries (no chain fields) get counted as legacy, not failures."""
    path = history_path(tmp_path)
    legacy_entries = [
        {
            "id": "leg1",
            "timestamp": 100.0,
            "request": "legacy 1",
            "plan": [],
            "outcomes": [],
        },
        {
            "id": "leg2",
            "timestamp": 200.0,
            "request": "legacy 2",
            "plan": [],
            "outcomes": [],
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in legacy_entries:
            f.write(json.dumps(e) + "\n")

    status = verify_chain(tmp_path)
    assert status.ok
    assert status.legacy_count == 2
    assert status.chained_count == 0


def test_verify_mixed_legacy_then_chained(tmp_path: Path) -> None:
    """Common upgrade scenario: a few legacy entries, then v0.7+ entries."""
    path = history_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a legacy entry by hand
    legacy = {
        "id": "old", "timestamp": 100.0, "request": "old r",
        "plan": [], "outcomes": [],
    }
    with path.open("w") as f:
        f.write(json.dumps(legacy) + "\n")
    # Append two chained entries via the normal API.
    append_history(_make_entry(0), tmp_path)
    append_history(_make_entry(1), tmp_path)

    status = verify_chain(tmp_path)
    assert status.ok
    assert status.legacy_count == 1
    assert status.chained_count == 2


# --- end-to-end: ask → history → verify ----------------------------------


@pytest.mark.asyncio
async def test_real_ask_pipeline_writes_verifiable_chain(tmp_path: Path) -> None:
    """Going through the real Nation.ask path should produce a valid chain."""
    from dataclasses import dataclass as _dc
    from anthill.core.agent import Agent
    from anthill.core.history import build_entry_from_ask
    from anthill.core.nation import Nation

    @_dc
    class _R:
        text: str = "ok"
        input_tokens: int = 1
        output_tokens: int = 1

    class _P:
        async def complete(self, *args, **kwargs):  # noqa: ANN001, ANN201, ARG002
            return _R()

    n = Nation(name="t")
    n.use_judge = False
    a = Agent(id="ant-1", model="x")
    a._provider = _P()  # type: ignore[assignment]
    n.agents = [a]

    # Simulate 3 ask results landing in history
    for i in range(3):
        result = await n.run("x", f"prompt {i}")
        # Build the entry the way nation.ask would
        from anthill.core.executor import SubtaskOutcome
        from anthill.core.scout import Subtask
        outcome = SubtaskOutcome(
            subtask=Subtask("x", f"prompt {i}", []),
            attempts=[result],
            status="ok",
        )
        entry = build_entry_from_ask(f"req {i}", [outcome.subtask], [outcome])
        append_history(entry, tmp_path)

    status = verify_chain(tmp_path)
    assert status.ok
    assert status.chained_count == 3
