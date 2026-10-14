"""Artifact-backed table partitions with one manifest defining row order.

Architecture: parts_path -> manifest entries -> lazily decoded Tables -> global
row indices. Run logging materializes those same immutable parts for the existing
table viewer; JoinedTable uses that materialization without another data store.
Main callers: Artifact.add/get, Encoder, JoinedTable and public import aliases.
Partition iteration follows W&B 0.30.0 table.py (MIT, THIRD_PARTY_NOTICES.md).
No logged path can select files outside its owning artifact manifest.
"""
from __future__ import annotations

import json
import copy

from .media_base import Media


class _PartitionTablePartEntry:
    def __init__(self, entry, source_artifact):
        self.entry, self.source_artifact = entry, source_artifact
        self._part = None

    def get_part(self):
        if self._part is None:
            self._part = self.source_artifact.get(self.entry.path)
        return self._part

    def free(self):
        self._part = None


class PartitionedTable(Media):
    """A directory of table parts in one immutable artifact."""

    _log_type = "partitioned-table"

    def __init__(self, parts_path):
        super().__init__()
        self.parts_path = parts_path
        self._loaded_part_entries = {}
        self._artifact_source = self._artifact_target = None

    def to_json(self, artifact_or_run):
        from .run import Run
        result = {"_type": self._log_type}
        if isinstance(artifact_or_run, Run):
            reference = self._get_artifact_entry_ref_url()
            if reference is None:
                raise ValueError("PartitionedTables must first be added to an Artifact before logging to a Run")
            result["artifact_path"] = reference
        else:
            from .artifacts import Artifact
            source = self._artifact_source or self._artifact_target
            if isinstance(artifact_or_run, Artifact) and source is not None and source is not artifact_or_run:
                self._copy_parts(source, artifact_or_run)
            result["parts_path"] = self.parts_path
        return result

    def _copy_parts(self, source, destination):
        """Copy part values and dependencies without changing their row order.

        Main callers: to_json during Artifact.add. The destination may already
        contain identical parts, but unrelated files in this directory would
        change the table and must fail. Outer Artifact.add restores the entire
        manifest after any conflict; scratch files never become committed data.
        """
        from .tables import Table
        entries = source.manifest.get_entries_in_directory(self.parts_path)
        paths = [entry.path for entry in entries]
        existing_paths = [entry.path for entry in destination.manifest.get_entries_in_directory(self.parts_path)]
        if any(path not in paths for path in existing_paths):
            raise ValueError(f"Conflicting partition contents in directory {self.parts_path}")
        for entry in entries:
            existing = destination.manifest.entries.get(entry.path)
            if existing is not None and existing.sha256 != entry.sha256:
                raise ValueError(f"Conflicting partition contents at {entry.path}")
            part = source.get(entry.path)
            if not isinstance(part, Table):
                raise ValueError(f"Partition is not a Table: {entry.path}")
            # Older native EvalTable snapshots used the ordinary Table suffix.
            # Preserve that immutable logical name while copying its roles.
            if entry.path.endswith(".table.json") and part._log_type != "table":
                part = copy.copy(part)
                part._log_type = "table"
            destination.add(part, entry.path)
        ordered, inserted = {}, False
        for path, entry in destination._entries.items():
            if path in paths:
                if not inserted:
                    ordered.update((name, destination._entries[name]) for name in paths)
                    inserted = True
            else:
                ordered[path] = entry
        destination._entries = ordered

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        result = cls(json_obj["parts_path"])
        result._artifact_source = source_artifact
        for entry in source_artifact.manifest.get_entries_in_directory(result.parts_path):
            result._add_part_entry(entry, source_artifact)
        return result

    def _add_part_entry(self, entry, source_artifact):
        self._loaded_part_entries[entry.path] = _PartitionTablePartEntry(entry, source_artifact)

    def iterrows(self):
        """Yield consecutive indices while checking every part's column order.

        Main callers: user iteration and native materialization. Empty parts
        still establish a schema. The iteration order is the saved manifest's
        entry order, matching browser reconstruction of that same artifact.
        """
        columns, index = None, 0
        for entry in self._loaded_part_entries.values():
            part = entry.get_part()
            if columns is None:
                columns = part.columns
            elif columns != part.columns:
                raise ValueError(f"Table parts have non-matching columns. {columns} != {part.columns}")
            try:
                for _, row in part.iterrows():
                    yield index, row
                    index += 1
            finally:
                entry.free()

    def _materialize(self, owner=None):
        """Build the browser's table value from the exact owning manifest.

        Main callers: Encoder and JoinedTable.materialize. A fresh constructor
        has no loaded parts upstream; this internal path resolves its committed
        owner before preparing a run snapshot. Descriptive edits cannot replace
        the immutable source files selected here.
        """
        from .tables import Table
        owner = owner or self._artifact_source or self._artifact_target
        if owner is None:
            raise ValueError("PartitionedTable requires its source artifact")
        source = self if self._loaded_part_entries else self.from_json(self.to_json(None), owner)
        columns = None
        rows, metadata = [], []
        for wrapper in source._loaded_part_entries.values():
            part = wrapper.get_part()
            if not isinstance(part, Table):
                raise ValueError(f"Partition is not a Table: {wrapper.entry.path}")
            if columns is None:
                columns = list(part.columns)
            elif columns != part.columns:
                raise ValueError(f"Table parts have non-matching columns. {columns} != {part.columns}")
            try:
                raw = json.loads(wrapper.entry._read_verified_bytes())
                metadata.append({"path": wrapper.entry.path, "row_offset": len(rows), "row_count": len(part.data),
                                 "column_types": raw.get("column_types", []),
                                 **({"evaluation_roles": raw["evaluation_roles"]} if "evaluation_roles" in raw else {})})
                rows.extend(row for _, row in part.iterrows())
            finally:
                wrapper.free()
        table = Table(columns=[] if columns is None else columns, data=rows, allow_mixed_types=True)
        table._partition_info = {"parts_path": self.parts_path, "partition_parts": metadata}
        return table

    def bind_to_run(self, *args, **kwargs):
        raise ValueError("PartitionedTables cannot be bound to runs")

    @classmethod
    def get_media_subdir(cls):
        raise NotImplementedError

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.parts_path == other.parts_path

    def __ne__(self, other):
        return not self.__eq__(other)


from . import table_types as _dtypes

class _PartitionedTableType(_dtypes.Type):
    name = "partitioned-table"
    types = [PartitionedTable]

_dtypes.TypeRegistry.add(_PartitionedTableType)
