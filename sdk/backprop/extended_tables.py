"""Joined and evaluation tables on the existing immutable table storage.

Architecture: validate references/column roles -> store ordinary table data plus
typed metadata -> render through the table explorer. JoinedTable retains both
source tables in its artifact. EvalTable is re-exported from its separate
publication module, which replaces Weave storage with Foundry evaluation records.
Main callers: public constructors, Encoder and artifact object codecs.
"""
from .tables import Table
from .values import WBValue
from .media_base import Media
from .partitioned_table import PartitionedTable


class JoinedTable(Media):
    _log_type = "joined-table"

    def __init__(self, table1, table2, join_key):
        super().__init__()
        if not (isinstance(join_key, str) or isinstance(join_key, list) and len(join_key) == 2):
            raise ValueError("join_key must be a string or pair of strings")
        for table in (table1, table2):
            if not isinstance(table, (Table, PartitionedTable)) and not (isinstance(table, str) and table.endswith(".table.json")) and not (hasattr(table, "ref_url") and table.ref_url().endswith(".table.json")):
                raise ValueError("JoinedTable inputs must be Tables, PartitionedTables or artifact table references")
        self._table1, self._table2, self._join_key = table1, table2, join_key

    def _ensure_table_in_artifact(self, table, artifact, index):
        """Copy a join input and its dependencies into the receiving artifact.

        Main callers: to_json. A bare JSON reference is insufficient when cells
        or schemas refer to other files. Native entries are decoded through
        their authenticated owner and re-encoded using the existing atomic
        Artifact.add transaction. No extra storage or cross-artifact UI fetch is
        needed. Generic reference objects use the declared reference interface.
        """
        import base64
        import os
        from .artifacts import ArtifactManifestEntry
        if isinstance(table, (Table, PartitionedTable)):
            source_name = getattr(table, "_artifact_source_name", None)
            name = os.path.basename(source_name) if source_name is not None else f"t{index}_{id(self)}"
            path = artifact.add(table, name).path
        elif hasattr(table, "ref_url"):
            name = base64.standard_b64decode(table.digest).hex()[:20] + "." + table.name.split(".")[-2] + ".json"
            if isinstance(table, ArtifactManifestEntry):
                source = table.parent_artifact()
                value = source.get(table.path)
                if not isinstance(value, (Table, PartitionedTable)):
                    raise TypeError("JoinedTable reference must contain a Table")
                entry = artifact.add(value, name)
                artifact._set_entry_provenance(entry, ref=table.ref_url(), birth_artifact_id=table.birth_artifact_id or source.id)
                path = entry.path
            else:
                path = artifact.add_reference(table.ref_url(), name)[0].path
        else:
            path = table
        if path not in artifact.manifest.entries:
            raise ValueError(f"JoinedTable table:{path} not found in artifact; add that table first")
        return path

    def to_json(self, artifact_or_run):
        from .run import Run
        if isinstance(artifact_or_run, Run):
            reference = self._get_artifact_entry_ref_url()
            if reference is None:
                raise ValueError("JoinedTables must first be added to an Artifact before logging to a Run")
            return {"_type": self._log_type, "artifact_path": reference}
        paths = [self._ensure_table_in_artifact(table, artifact_or_run, index)
                 for index, table in enumerate((self._table1, self._table2), 1)]
        return {"_type": self._log_type, "table1": paths[0], "table2": paths[1], "join_key": self._join_key}

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        inputs = []
        for key in ("table1", "table2"):
            value = source_artifact.get(json_obj[key])
            inputs.append(json_obj[key] if value is None else value)
        result = cls(*inputs, json_obj["join_key"])
        result._artifact_source = source_artifact
        return result

    def materialize(self):
        """Build a full outer join, preserving duplicate-key combinations.

        Main callers: run logging and table explorer descriptors. Unmatched rows
        contain None on the missing side rather than silently disappearing.
        """
        owner = getattr(self, "_artifact_source", None) or getattr(self, "_artifact_target", None)
        tables = []
        for value in (self._table1, self._table2):
            if isinstance(value, PartitionedTable): tables.append(value._materialize(owner))
            elif isinstance(value, Table): tables.append(value)
            elif isinstance(value, str) and owner is not None: tables.append(owner.get(value))
            elif hasattr(value, "parent_artifact"): tables.append(value.parent_artifact().get(value.path))
            else: raise ValueError("JoinedTable reference requires its source artifact")
        tables = [value._materialize(owner) if isinstance(value, PartitionedTable) else value for value in tables]
        left, right = tables
        keys = [self._join_key] * 2 if isinstance(self._join_key, str) else self._join_key
        li, ri = left.columns.index(keys[0]), right.columns.index(keys[1])
        output, matched = [], set()
        index_by_key = {}
        for index, b in enumerate(right.data):
            try: index_by_key.setdefault(b[ri], []).append((index,b))
            except TypeError: raise ValueError("JoinedTable keys must be scalar hashable values") from None
        for a in left.data:
            try: found = index_by_key.get(a[li], [])
            except TypeError: raise ValueError("JoinedTable keys must be scalar hashable values") from None
            if not found: output.append([*a, *([None] * len(right.columns))])
            for index, b in found:
                matched.add(index)
                output.append([*a, *b])
        output.extend([*([None] * len(left.columns)), *b] for index, b in enumerate(right.data) if index not in matched)
        return Table(columns=[*(f"left.{c}" for c in left.columns), *(f"right.{c}" for c in right.columns)],
                     data=output, allow_mixed_types=True)

    def bind_to_run(self, *args, **kwargs):
        raise ValueError("JoinedTables cannot be bound to runs")

    @classmethod
    def get_media_subdir(cls):
        # The pinned inherited Media method is abstract for JoinedTable too.
        raise NotImplementedError


# Keep the established import path while separating evaluation publication.
from .eval_table import EvalTable
