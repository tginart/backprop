"""Run-file snapshots and safe restoration through the Foundry store.

Architecture: save resolves a fixed set of caller files -> links into run.dir
-> stages immutable upload snapshots -> emits file records. Live/end policies
observe the returned local paths, so caller edits and source-link edits share
the same bytes. If linking is unavailable, a copy uses pinned live-to-now policy
downgrade with a warning. Restore validates a relative name before reading.
Main callers: Run.save/restore/finish/background flush and public file APIs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import glob as glob_module
import fnmatch
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from .errors import UsageError


@dataclass(frozen=True)
class RunStatus:
    sync_items_total: int = 0
    sync_items_pending: int = 0
    sync_time: datetime | None = None


def logical_path(name):
    value = Path(name)
    if value.is_absolute() or not value.parts or any(part in {"..", "."} for part in value.parts):
        raise UsageError("Run file name must be a safe relative path")
    return value


class RunFiles:
    def __init__(self, run):
        self.run = run
        self.tracked = {}

    def _destination(self, name):
        """Confine writable parent directories while permitting the file link.

        Main callers: save. The final path may point to a caller-authorized
        source outside the run directory; its containing directory must stay
        within run.dir, including when a preexisting parent is a symlink.
        """
        root = Path(self.run.dir).resolve()
        destination = root / logical_path(name)
        if not destination.parent.resolve().is_relative_to(root):
            raise UsageError("Saved run file parent escapes the run directory")
        return destination

    def _materialize(self, source, destination, policy):
        """Create the pinned symlink/hardlink/copy local saved-file view.

        Main callers: save after validating every requested path. Uploads still
        copy immutable bytes into the journal; local links are only the live
        user-facing files. Copy fallback cannot observe source edits and therefore
        explicitly downgrades live to now, as pinned W&B does.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and source.samefile(destination):
            return policy
        if destination.is_dir():
            raise IsADirectoryError(destination)
        destination.unlink(missing_ok=True)
        if self.run._settings.symlink:
            try:
                destination.symlink_to(source)
                return policy
            except (OSError, NotImplementedError):
                pass
        try:
            os.link(source, destination)
            return policy
        except (OSError, NotImplementedError):
            pass
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
        try:
            shutil.copy2(source, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        if policy == "live":
            self.run._warn("Saved file could not be linked; copied snapshot uses policy='now' instead of 'live'")
            return "now"
        return policy

    def save(self, glob_str, base_path=None, policy="live", glob=True):
        if policy not in {"live", "now", "end"}:
            raise ValueError("policy must be live, now, or end")
        if isinstance(glob_str, bytes):
            glob_str = glob_str.decode()
        pattern = os.fspath(glob_str)
        if "://" in pattern:
            raise ValueError("Run.save supports local files; use an Artifact reference for remote URIs")
        matches = glob_module.glob(pattern, recursive=True) if glob else [pattern]
        if base_path is None:
            original = Path(pattern)
            base = original.parent.parent if original.is_absolute() else Path.cwd()
        else:
            base = Path(base_path)
        base = base.resolve()
        try:
            relative_pattern = Path(pattern).resolve().relative_to(base)
        except ValueError as error:
            raise ValueError("glob_str must be inside base_path") from error
        candidates = {}
        for match in matches:
            source = Path(match).resolve()
            if not source.is_file():
                continue
            if not source.is_relative_to(base):
                raise ValueError("glob_str must be inside base_path")
            name = source.relative_to(base).as_posix()
            logical_path(name)
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in self.run._settings.ignore_globs):
                continue
            candidates[name] = (source, self._destination(name))
        # Pinned save also returns existing run-directory matches, even after
        # an original source disappears. A hardlink remains usable then. A
        # dangling symlink cannot supply bytes and errors before any writes.
        root = Path(self.run.dir).resolve()
        existing = root.glob(relative_pattern.as_posix()) if glob else [root / relative_pattern]
        for destination in existing:
            name = destination.relative_to(root).as_posix()
            if name in candidates or any(fnmatch.fnmatchcase(name, ignored) for ignored in self.run._settings.ignore_globs):
                continue
            destination = self._destination(name)
            if destination.is_symlink() and not destination.exists():
                raise FileNotFoundError(f"Saved run file is a broken symlink: {destination}")
            if destination.is_file():
                candidates[name] = (destination, destination)
        outputs = []
        for name, (source, destination) in sorted(candidates.items()):
            effective = self._materialize(source, destination, policy)
            previous = self.tracked.get(name, {})
            self.tracked[name] = {"source": source, "destination": destination, "policy": effective,
                                  "digest": previous.get("digest")}
            if effective != "end":
                self._snapshot(name)
            outputs.append(str(destination))
        return outputs

    def _snapshot(self, name):
        item = self.tracked[name]
        destination = item["destination"]
        if not destination.is_file():
            raise FileNotFoundError(f"Tracked run file disappeared: {destination}")
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if digest == item["digest"]:
            return
        reference = self.run._journal.stage_file(destination, name=name)
        self.run._emit("file", {"name": name, "file": reference, "policy": item["policy"]})
        item["digest"] = reference["sha256"]

    def poll(self, ending=False):
        for name, item in list(self.tracked.items()):
            if item["policy"] == "live" or (ending and item["policy"] == "end"):
                self._snapshot(name)


def restore_file(store, events, name, *, replace=False, root=None):
    """Resolve the newest matching file record and atomically download its bytes."""
    relative = logical_path(name)
    directory = Path(root or os.getcwd()).resolve()
    destination = (directory / relative).resolve()
    if not destination.is_relative_to(directory):
        raise UsageError("Run file target escapes the requested directory")
    if destination.is_file() and not replace:
        return destination.open("r")
    references = [event["payload"]["file"] for event in events if event["type"] == "file" and event["payload"].get("name") == name]
    if not references:
        return None
    reference = references[-1]
    data = store.download_file(reference)
    if not isinstance(data, bytes):
        raise TypeError("download_file must return bytes")
    if reference.get("sha256") and hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("Downloaded run file checksum mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        output.write(data)
        temporary = Path(output.name)
    temporary.replace(destination)
    return destination.open("r")
