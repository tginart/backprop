"""
Purpose: Import durable journal prefixes without a change to the source files.
Input: Existing journal directories and a local HTTP store.
Output: Committed local events and a separate local ingestion descriptor.
Note: Source records, Foundry acknowledgements, and resolution caches stay intact.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
import re
import shutil
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backprop.transport import Journal
    from .store import LocalStore


def discover_journals(paths: list[str]) -> list[Path]:
    """
    Purpose: Find journals only beneath the directories the user supplies.
    Input: Journal directories, events.jsonl files, or parent directories.
    Output: A sorted list of distinct journal directories.
    """
    directories = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if path.is_file() and path.name == "events.jsonl":
            directories.add(path.parent)
        elif (path / "events.jsonl").is_file():
            directories.add(path)
        elif path.is_dir():
            directories.update(event.parent for event in path.rglob("events.jsonl"))
        else:
            raise ValueError("Import requires a journal or an existing parent directory")
    if not directories:
        raise ValueError("No events.jsonl journals found in the supplied paths")
    return sorted(directories)


def import_journal(directory: str | Path, store: LocalStore) -> int:
    """
    Purpose: Replay an immutable source snapshot with a separate local cursor.
    Input: A source journal directory and a LocalStore.
    Output: The number of complete source records after successful ingestion.
    Method:
        1. Check the previously imported source prefix.
        2. Copy complete records and staged files into an isolated snapshot.
        3. Use the existing journal uploader and artifact resolution claims.
        4. Commit the local ingestion descriptor after all uploads complete.
    Note: An incomplete final line waits for the next import.
    """
    from backprop.transport import Journal
    source = Path(directory).expanduser().resolve()
    identifier = hashlib.sha256(str(source).encode()).hexdigest()
    endpoint = "/api/backprop/imports/" + identifier
    previous = store.request(endpoint, retry=True)
    with tempfile.TemporaryDirectory(prefix="backprop-import-") as temporary:
        snapshot = Path(temporary)
        state = _snapshot_records(source, snapshot, previous)
        for name in ("files", "media", "live-media"):
            if (source / name).is_dir():
                shutil.copytree(source / name, snapshot / name, symlinks=True)
        intent = source / "branch-intent.json"
        if intent.is_file():
            shutil.copyfile(intent, snapshot / intent.name)
            _restore_branch_snapshot(snapshot, store)
        journal = Journal(snapshot, sink=store)
        _restore_live_media(journal, identifier)
        journal.flush()
        store.request(endpoint, "POST", state, retry=True)
    return state["count"]


def _restore_live_media(journal: Journal, source_id: str) -> None:
    """
    Purpose: Restore installation-owned media choices before journal publication.
    Input: The isolated journal snapshot and the source journal identity.
    Output: Immutable staged bytes from the first durable claim for each binding.
    Note: Concurrent importers use the same claim before they publish an event.
    """
    from backprop.live_media import _under, _write
    from backprop.transport import BackpropError, read_journal
    store = journal.sink
    events = read_journal(journal.directory, project_artifacts=False)
    restored = set()
    for reference in _live_media_references(events):
        identity, name = reference.get("_backprop_live_media"), reference.get("name")
        if (not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None
                or not isinstance(name, str) or name in {"", ".", ".."}
                or Path(name).name != name):
            raise BackpropError("Invalid imported live media reference")
        if identity in restored:
            continue
        scope = {"kind": "local-journal-live-media", "source_id": source_id,
                 "binding_id": identity, "name": name}
        key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        winner = store.read_artifact_resolution(key)
        if winner is None:
            snapshot = journal.freeze_live_media(reference)
            uploaded = journal._resolve(snapshot)
            winner = store.claim_artifact_resolution(key, {**scope, "file": uploaded})
        if any(winner.get(field) != value for field, value in scope.items()):
            raise BackpropError("Imported live media claim has a conflicting scope")
        data = store.download_file(winner["file"])
        digest = hashlib.sha256(data).hexdigest()
        _under(journal.directory, f"files/{digest}/{name}")
        snapshot = journal.stage_file(None, data=data, name=name)
        choice = _under(journal.directory, f"live-media/{identity}.snapshot.json")
        if choice.is_symlink():
            raise BackpropError("Frozen live media metadata cannot be a symlink")
        _write(choice, snapshot)
        journal.freeze_live_media(reference)
        restored.add(identity)


def _live_media_references(value: object) -> Iterator[dict]:
    """
    Purpose: Find linked media in nested event payloads without a record change.
    Input: The complete journal records or one nested value.
    Output: Each queued live media reference in source order.
    """
    if isinstance(value, dict):
        if "_backprop_live_media" in value:
            yield value
        for child in value.values():
            yield from _live_media_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _live_media_references(child)


def _restore_branch_snapshot(directory: Path, store: LocalStore) -> None:
    """
    Purpose: Retain one local parent choice across branch imports and retries.
    Input: The isolated journal snapshot and the local HTTP store.
    Output: A branch snapshot from an atomic server-side claim.
    Note: The source journal's Foundry branch cache is never read or changed.
    """
    from backprop.deferred_branch import INTENT, SNAPSHOT, _atomic_json, _freeze_parent
    from backprop.transport import BackpropError
    intent = json.loads((directory / INTENT).read_text())
    identity = json.dumps(["local-journal-branch", intent], sort_keys=True).encode()
    key = hashlib.sha256(identity).hexdigest()
    winner = store.read_artifact_resolution(key)
    if winner is None:
        originals = [json.loads(line) for line in
                     (directory / "events.jsonl").read_text().splitlines() if line.strip()]
        if not originals:
            return
        try:
            candidate = _freeze_parent(directory, intent, originals, store)
        except BackpropError:
            winner = store.read_artifact_resolution(key)
            if winner is None:
                raise
        else:
            winner = store.claim_artifact_resolution(key, candidate)
    _atomic_json(directory / SNAPSHOT, winner)


def _snapshot_records(source: Path, snapshot: Path, previous: dict) -> dict:
    """
    Purpose: Copy a complete prefix and detect a change to an earlier import.
    Input: The source directory, snapshot directory, and local import descriptor.
    Output: The record count, byte offset, and prefix SHA-256.
    """
    digest = hashlib.sha256()
    offset, count = 0, 0
    checkpoint = previous.get("byte_offset", 0)
    verified = not checkpoint
    with (source / "events.jsonl").open("rb") as reader:
        with (snapshot / "events.jsonl").open("wb") as writer:
            for line in reader:
                if not line.endswith(b"\n"):
                    break
                if line.strip():
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Journal records must be JSON objects")
                    count += 1
                writer.write(line)
                digest.update(line)
                offset += len(line)
                if offset == checkpoint:
                    if digest.hexdigest() != previous["sha256"]:
                        raise ValueError("Previously imported journal records changed")
                    verified = True
    if not verified:
        raise ValueError("Previously imported journal prefix is absent or incomplete")
    return {"count": count, "byte_offset": offset, "sha256": digest.hexdigest()}
