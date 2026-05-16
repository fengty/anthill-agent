"""History — every ask gets a permanent record.

Until now, the nation kept only the *last* ask (so 'anthill rate' had a
target). That's enough for immediate feedback but loses everything past
the most recent request. A nation that cannot remember what it has done
cannot grow institutional memory.

Each ask appends an HistoryEntry to history.jsonl — newline-delimited
JSON, easy to inspect, easy to grep, never rewritten. The CLI exposes:

    anthill history          list recent entries
    anthill history show ID  print the full trace for one entry
    anthill history search Q grep across requests
    anthill history verify   v0.7+: walk the hash chain, report integrity

The id is the first 8 chars of a sha256 of (request + timestamp), so
listing shows short stable handles. No more guessing which one was
yesterday.

v0.7 — Hash chain. Each new entry references the prior entry's hash
(prev_hash field) so any post-hoc tampering with an earlier line breaks
the chain at the tampered point. The chain is a Merkle-style spine,
not a signed certificate: it proves *internal consistency*, not
authorship. Optional Ed25519 signing is planned for v0.7.1.

Why this matters for v0.8 (federation): when one nation imports
another nation's "experience pack," it has to be able to verify the
history segment hasn't been edited after the fact. The hash chain is
the cheap version of that guarantee.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path


# v0.7 hash-chain constants. The version string travels with each
# entry so a future format can be distinguished cleanly.
CHAIN_VERSION = "v1"
GENESIS_PREV_HASH = "0" * 64  # sha256-shaped placeholder for the first entry


def _entry_hash(entry: "HistoryEntry") -> str:
    """sha256 over the JSON view of an entry's content (excluding `chain_hash`).

    The hash covers: chain_version, prev_hash, id, timestamp, request,
    plan, outcomes. The entry's own `chain_hash` is the OUTPUT of this
    function — it must not be part of the input or hashing becomes
    self-referential.
    """
    payload = {
        "chain_version": CHAIN_VERSION,
        "prev_hash": entry.prev_hash,
        "id": entry.id,
        "timestamp": entry.timestamp,
        "request": entry.request,
        "plan": entry.plan,
        "outcomes": entry.outcomes,
    }
    # sort_keys ensures the same dict serializes to the same bytes
    # regardless of insertion order, so hashes are reproducible across
    # Python versions / dict implementations.
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass
class HistoryEntry:
    id: str
    timestamp: float
    request: str
    plan: list[dict]  # serialized subtasks: {task_type, depends_on}
    outcomes: list[dict] = field(default_factory=list)  # status + final output per subtask
    # v0.7+ hash-chain fields. `prev_hash` references the previous
    # entry's chain_hash, GENESIS_PREV_HASH for the first entry. Older
    # files without these fields load with empty strings and
    # `verify_chain` reports them as "legacy" — not a failure, just
    # outside the protected window.
    prev_hash: str = ""
    chain_hash: str = ""
    chain_version: str = ""
    # v0.7.1+ — when set, this ask was produced by a background job.
    # The job_id flows in through the ANTHILL_BG_JOB_ID env var that
    # core/background.py sets when spawning the child process.
    bg_job_id: str | None = None

    @staticmethod
    def make_id(request: str, timestamp: float) -> str:
        return hashlib.sha256(f"{request}{timestamp}".encode()).hexdigest()[:8]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "request": self.request,
            "plan": self.plan,
            "outcomes": self.outcomes,
            "chain_version": self.chain_version,
            "prev_hash": self.prev_hash,
            "chain_hash": self.chain_hash,
            "bg_job_id": self.bg_job_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            id=data["id"],
            timestamp=data["timestamp"],
            request=data["request"],
            plan=data.get("plan", []),
            outcomes=data.get("outcomes", []),
            prev_hash=str(data.get("prev_hash") or ""),
            chain_hash=str(data.get("chain_hash") or ""),
            chain_version=str(data.get("chain_version") or ""),
            bg_job_id=data.get("bg_job_id"),
        )


def history_path(nation_dir: Path) -> Path:
    return nation_dir / "history.jsonl"


def _last_chain_hash(path: Path) -> str:
    """Pull the chain_hash from the last line of history.jsonl, or genesis.

    Reads the whole file because history.jsonl is line-delimited but
    nothing in the format records the file size or last offset. The
    cost is fine for hundreds of entries; if you have a million you
    should rebuild the chain via a separate offline pass.
    """
    if not path.exists():
        return GENESIS_PREV_HASH
    with path.open() as f:
        last_chain = GENESIS_PREV_HASH
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            ch = data.get("chain_hash") or ""
            if ch:
                last_chain = ch
        return last_chain


def append_history(entry: HistoryEntry, nation_dir: Path) -> None:
    """Append the entry with hash-chain fields populated automatically."""
    nation_dir.mkdir(parents=True, exist_ok=True)
    path = history_path(nation_dir)
    # Wire up the chain: prev_hash points at the previous tail.
    entry.prev_hash = _last_chain_hash(path)
    entry.chain_version = CHAIN_VERSION
    entry.chain_hash = _entry_hash(entry)
    with path.open("a") as f:
        f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")


def load_history(nation_dir: Path, *, limit: int | None = None) -> list[HistoryEntry]:
    path = history_path(nation_dir)
    if not path.exists():
        return []
    entries: list[HistoryEntry] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(HistoryEntry.from_dict(json.loads(line)))
    if limit is not None:
        entries = entries[-limit:]
    return entries


def find_by_id(entry_id: str, nation_dir: Path) -> HistoryEntry | None:
    for entry in load_history(nation_dir):
        if entry.id.startswith(entry_id):  # prefix match — short ids
            return entry
    return None


def search_history(query: str, nation_dir: Path) -> list[HistoryEntry]:
    needle = query.lower()
    return [e for e in load_history(nation_dir) if needle in e.request.lower()]


def find_by_bg_job(bg_job_id: str, nation_dir: Path) -> list[HistoryEntry]:
    """v0.7.1 — return every history entry produced by this bg job.

    Used by `anthill bg show` to surface the matching ask records so
    a user looking at a bg job's terminal output can also see the
    structured outcome that went into the nation's memory.
    """
    return [
        e for e in load_history(nation_dir)
        if e.bg_job_id and e.bg_job_id == bg_job_id
    ]


def build_entry_from_ask(
    request: str,
    plan_subtasks: list,  # list of Subtask
    outcomes: list,  # list of SubtaskOutcome
) -> HistoryEntry:
    import os
    ts = time.time()
    # If this process was spawned by `anthill bg ask`, the parent set
    # ANTHILL_BG_JOB_ID so we can back-reference. Foreground asks have
    # this unset and bg_job_id stays None — same JSON shape, just empty.
    bg_job_id = os.environ.get("ANTHILL_BG_JOB_ID") or None
    return HistoryEntry(
        id=HistoryEntry.make_id(request, ts),
        timestamp=ts,
        request=request,
        bg_job_id=bg_job_id,
        plan=[
            {"task_type": s.task_type, "depends_on": list(s.depends_on)}
            for s in plan_subtasks
        ],
        outcomes=[
            {
                "task_type": o.subtask.task_type,
                "status": o.status,
                "attempts": len(o.attempts),
                "final_output": o.output if o.status == "ok" else None,
                "skip_reason": o.skip_reason,
                # agent_id of the final attempt — None when the subtask was
                # skipped before any citizen ran it. Used by the lifecycle
                # auditor (v0.3.0) to credit recent activity to specific
                # citizens; older history files predate this field and the
                # auditor falls back to pheromone timestamps for them.
                "agent_id": o.final.agent_id if o.final is not None else None,
                # Structured failure attribution per attempt (v0.5+). Stored
                # as a list of FailureReason value strings; len matches
                # the number of attempts. Successful attempts contribute None.
                "failure_reasons": [
                    getattr(a, "failure_reason", None) for a in o.attempts
                ],
            }
            for o in outcomes
        ],
    )


# --- v0.7 — chain verification --------------------------------------------


@dataclass
class ChainStatus:
    """Outcome of a verify_chain run.

    `legacy_count` is entries that predate v0.7 — they have empty
    chain_hash / prev_hash and are reported as legacy, not as failures.
    `broken_at_index` is the first 0-based index where the chain
    diverges from what was recorded; -1 means clean.
    """

    total: int
    legacy_count: int
    chained_count: int
    broken_at_index: int = -1
    broken_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.broken_at_index == -1


def verify_chain(nation_dir: Path) -> ChainStatus:
    """Walk the history file, re-compute each entry's hash, find tampering.

    For every entry that carries a `chain_hash`:
      - recomputed_hash must equal stored chain_hash
      - prev_hash must equal the previous entry's chain_hash (or
        GENESIS_PREV_HASH for the first chained entry)

    Entries with no chain_hash (legacy v0.6 and earlier) are skipped
    and counted under `legacy_count`. Mixed files — legacy at the start
    then chained — are common during the upgrade and treated correctly.
    """
    entries = load_history(nation_dir)
    total = len(entries)
    if total == 0:
        return ChainStatus(total=0, legacy_count=0, chained_count=0)

    legacy = 0
    chained = 0
    prev_chain = GENESIS_PREV_HASH

    for i, entry in enumerate(entries):
        if not entry.chain_hash:
            legacy += 1
            continue
        # First chained entry resets the prev pointer to whatever it
        # claims its prev_hash is (GENESIS or the previous tail). After
        # that, each entry's prev_hash must equal the previous entry's
        # chain_hash.
        if chained == 0:
            # OK to anchor to whatever prev_hash this entry has.
            prev_chain = entry.prev_hash
        if entry.prev_hash != prev_chain:
            return ChainStatus(
                total=total,
                legacy_count=legacy,
                chained_count=chained,
                broken_at_index=i,
                broken_reason=(
                    f"prev_hash {entry.prev_hash[:12]}… does not match "
                    f"expected {prev_chain[:12]}…"
                ),
            )
        recomputed = _entry_hash(entry)
        if recomputed != entry.chain_hash:
            return ChainStatus(
                total=total,
                legacy_count=legacy,
                chained_count=chained,
                broken_at_index=i,
                broken_reason="recomputed hash does not match stored chain_hash",
            )
        chained += 1
        prev_chain = entry.chain_hash

    return ChainStatus(total=total, legacy_count=legacy, chained_count=chained)
