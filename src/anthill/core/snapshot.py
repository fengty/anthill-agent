"""Snapshot — turn a nation directory into a single shareable archive.

A nation grown over weeks of use accumulates pheromones, culture,
history, exemplars, and plan cache that together represent real value.
The king should be able to:

    1. Back up the nation before a risky change.
    2. Move the nation to another machine.
    3. Hand a friend a copy of the nation as a tarball.
    4. Restore from a snapshot if state gets corrupted.

This module wraps the whole nation directory into a .tar.gz, and
unpacks it back. The format is plain tar — anything that can read it
will work, no proprietary metadata.

We also embed a small `snapshot.json` manifest at the archive root
with metadata (anthill version, citizen count, vocabulary size, time
of capture). That makes a snapshot self-describing: a tarball alone
tells you whose nation it is and what it knows.
"""

from __future__ import annotations

import json
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from anthill import __version__


@dataclass
class SnapshotManifest:
    anthill_version: str
    nation_name: str
    captured_at: float
    citizen_count: int
    vocabulary_size: int
    history_entries: int


MANIFEST_FILENAME = "snapshot.json"


def export_nation(nation_path: Path, output: Path) -> SnapshotManifest:
    """Bundle the nation directory into `output` (a .tar.gz path).

    Reads the nation dir directly — no need to load into memory. The
    manifest is computed from the persisted files so it always matches
    what's actually inside the archive.
    """
    if not nation_path.exists():
        raise FileNotFoundError(f"Nation directory not found: {nation_path}")

    manifest = _build_manifest(nation_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Write a temporary manifest file inside the nation dir before bundling.
    manifest_path = nation_path / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(_manifest_dict(manifest), indent=2))

    try:
        with tarfile.open(output, "w:gz") as tar:
            tar.add(nation_path, arcname=manifest.nation_name)
    finally:
        # Clean up the manifest file inside the source directory.
        if manifest_path.exists():
            manifest_path.unlink()

    return manifest


def import_nation(archive: Path, target_root: Path) -> SnapshotManifest:
    """Extract `archive` into `target_root`.

    The archive must contain exactly one top-level directory (the nation
    name) holding the same files a live nation would. Refuses to
    overwrite an existing nation of the same name — the king explicitly
    deletes first if they want a replacement.
    """
    if not archive.exists():
        raise FileNotFoundError(f"Snapshot not found: {archive}")
    target_root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise RuntimeError("Snapshot is empty.")
        top_levels = {m.name.split("/")[0] for m in members}
        if len(top_levels) != 1:
            raise RuntimeError(
                f"Snapshot must have exactly one top-level directory; got {top_levels!r}."
            )
        nation_name = next(iter(top_levels))
        if (target_root / nation_name).exists():
            raise FileExistsError(
                f"Nation '{nation_name}' already exists at {target_root}. "
                f"Delete it first or pick a different target."
            )
        tar.extractall(target_root)

    extracted = target_root / nation_name
    manifest_file = extracted / MANIFEST_FILENAME
    if manifest_file.exists():
        manifest_data = json.loads(manifest_file.read_text())
        manifest_file.unlink()  # clean up; manifest is metadata not state
        return SnapshotManifest(
            anthill_version=manifest_data.get("anthill_version", "unknown"),
            nation_name=manifest_data.get("nation_name", nation_name),
            captured_at=manifest_data.get("captured_at", 0.0),
            citizen_count=manifest_data.get("citizen_count", 0),
            vocabulary_size=manifest_data.get("vocabulary_size", 0),
            history_entries=manifest_data.get("history_entries", 0),
        )
    return _build_manifest(extracted)


def _build_manifest(nation_path: Path) -> SnapshotManifest:
    citizen_count = 0
    vocab = 0
    history = 0

    agents_file = nation_path / "agents.json"
    if agents_file.exists():
        citizen_count = len(json.loads(agents_file.read_text()))

    catalog = nation_path / "culture" / "catalog.json"
    if catalog.exists():
        vocab = len(json.loads(catalog.read_text()))

    history_file = nation_path / "history.jsonl"
    if history_file.exists():
        history = sum(1 for line in history_file.read_text().splitlines() if line.strip())

    return SnapshotManifest(
        anthill_version=__version__,
        nation_name=nation_path.name,
        captured_at=time.time(),
        citizen_count=citizen_count,
        vocabulary_size=vocab,
        history_entries=history,
    )


def _manifest_dict(m: SnapshotManifest) -> dict:
    return {
        "anthill_version": m.anthill_version,
        "nation_name": m.nation_name,
        "captured_at": m.captured_at,
        "citizen_count": m.citizen_count,
        "vocabulary_size": m.vocabulary_size,
        "history_entries": m.history_entries,
    }
