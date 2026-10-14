"""Typed logging values, independent of transport and optional ML libraries.

Architecture: Table/Histogram/Media/Artifact describe user data. Encoder turns
them into JSON values when a history row commits. Tables advance their upload
cursor only after that row is saved, so a rejected row never consumes data.
Files are copied into the run's durable queue before a remote upload is tried.
Main callers: Run.log, Run.log_artifact, and the plotting helpers.
"""
from __future__ import annotations

from datetime import date, datetime
from contextvars import ContextVar
import math
import mimetypes
import warnings

from .artifacts import Artifact, ArtifactManifestEntry
from .tables import Table, _optional_module
from .media import Media, BatchableMedia, Image, Audio, Video, Html, Plotly, Classes, BoundingBoxes2D, ImageMask, _is_matplotlib
from .values import WBValue
from .graph import Graph, Node, Edge
from .partitioned_table import PartitionedTable


_active_extensions = ContextVar("backprop_active_value_serializers", default=frozenset())


class Histogram(WBValue):
    """Preserve precomputed bins, or use NumPy's exact histogram behavior.

    Main callers: user logging, Encoder's large-array history conversion, and
    immutable artifact reconstruction. Like pinned W&B, automatic binning needs
    optional NumPy; precomputed counts and edges remain dependency free. Keeping
    one numerical implementation avoids changing boundary counts with imports.
    """

    MAX_LENGTH = 512
    _log_type = "histogram"

    def __init__(self, sequence=None, np_histogram=None, num_bins=64):
        if np_histogram:
            if len(np_histogram) != 2:
                raise ValueError("Expected np_histogram to be a tuple of (values, bin_edges) or sequence to be specified")
            counts, edges = np_histogram
            self.histogram = counts.tolist() if hasattr(counts, "tolist") else counts
            self.bins = edges.tolist() if hasattr(edges, "tolist") else edges
        else:
            try:
                numpy = _optional_module("numpy", "Automatic histogram calculation")
            except Exception as error:
                from .errors import Error
                raise Error("Auto creation of histograms requires numpy") from error
            counts, edges = numpy.histogram(sequence, bins=num_bins)
            self.histogram, self.bins = counts.tolist(), edges.tolist()
        if len(self.histogram) > self.MAX_LENGTH:
            raise ValueError(f"The maximum length of a histogram is {self.MAX_LENGTH}")
        if len(self.histogram) + 1 != len(self.bins):
            raise ValueError("len(bins) must be len(histogram) + 1")

    def to_json(self, run=None):
        return {"_type": self._log_type, "values": self.histogram, "bins": self.bins}

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        """Restore inline histogram values as a documented native extension.

        Main callers: WBValue registry dispatch and explicit public round trips.
        The pinned method is abstract, so decoding is additional behavior, not
        a claim that upstream accepts this call. Counts and edges need no file
        lookup; source_artifact is context for the common WBValue protocol.
        Constructor validation applies unchanged to malformed count/bin arrays.
        """
        return cls(np_histogram=(json_obj["values"], json_obj["bins"]))


class Encoder:
    """Convert a history row once and apply table cursors only after persistence."""

    def __init__(self, stage_file, context="value", settings=None, artifact_factory=None, table_cache=None, stage_media=None, stage_media_batch=None, media_cache=None, run=None):
        if context not in {"value", "history", "artifact", "table"}:
            raise ValueError("Encoder context must be value, history, artifact or table")
        self.run = run
        self.stage_file = stage_file
        self.stage_media = stage_media
        self.stage_media_batch = stage_media_batch
        self.media_cache = {} if media_cache is None else media_cache
        self.prepared_media = {}
        self.context = context
        self.settings = settings
        self.artifact_factory = artifact_factory
        self.artifacts = []
        self.key = "table"
        self.table_cache = {} if table_cache is None else table_cache
        self.prepared_tables = {}
        self.commits = []
        self._encoding_tables = set()
        self._preserve_sequences = True
        self._schema_artifact = None

    def encode(self, value):
        from .extended_tables import JoinedTable, EvalTable
        # Guard only the same value's super().to_json delegation. Context-local
        # state permits concurrent encoders and nested independent extensions.
        if (isinstance(value, WBValue) and not type(value).to_json.__module__.startswith("backprop.")
                and id(value) not in _active_extensions.get()):
            token = _active_extensions.set(_active_extensions.get() | {id(value)})
            try:
                owner = self._schema_artifact if self._schema_artifact is not None else self.run
                payload = value.to_json(owner)
                if not isinstance(payload, dict):
                    raise TypeError("WBValue.to_json must return a dictionary")
                return self._json_fields(payload)
            finally:
                _active_extensions.reset(token)
        if isinstance(value, JoinedTable):
            if not value._get_artifact_entry_ref_url():
                raise ValueError("JoinedTables must first be added to a logged artifact")
            return {**self.encode(value.materialize()), "joined_artifact": value._get_artifact_entry_ref_url()}
        if isinstance(value, PartitionedTable):
            if self.context in {"artifact", "table"} and self._schema_artifact is not None:
                return value.to_json(self._schema_artifact)
            reference = value._get_artifact_entry_ref_url()
            if reference is None:
                raise ValueError("PartitionedTables must first be added to an Artifact before logging to a Run")
            table = value._materialize()
            return {**self.encode(table), "partitioned_artifact": reference, "parts_path": value.parts_path}
        # A genuine installed pandas class (including subclasses) is converted
        # once, so the displayed table and implicit artifact share one identity.
        import sys
        pandas = sys.modules.get("pandas")
        if pandas is not None and isinstance(value, pandas.DataFrame):
            value = Table(dataframe=value)
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            return str(value)
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                return {"_type": "number", "value": "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"}
            return value
        if isinstance(value, (datetime, date)):
            return {"_type": "timestamp", "value": value.isoformat()}
        if isinstance(value, Artifact):
            return value._reference_json()
        if isinstance(value, Classes):
            payload = value.to_json(self._schema_artifact)
            return self.encode({**payload, "class_set": list(payload["class_set"])})
        if isinstance(value, BoundingBoxes2D):
            return self._json_fields({"_type": "bounding-boxes", **value._native_json()})
        if isinstance(value, EvalTable) and not value._native_evaluation_snapshot:
            from .evaluation import encode
            return encode(self, value)
        if isinstance(value, Table):
            from .table_construction import require_declared
            require_declared(value)
            cached_key = (id(value), 0 if value.log_mode == "IMMUTABLE" else value._generation)
            at_run_boundary = self.artifact_factory is not None and self.context in {"history", "value"}
            if at_run_boundary and cached_key in self.table_cache:
                cached = self.table_cache[cached_key]
                self.commits.extend(cached[1])
                self.artifacts.extend(cached[2])
                self.prepared_tables[cached_key] = cached
                return cached[0]
            first_commit, first_artifact = len(self.commits), len(self.artifacts)
            if at_run_boundary:
                descriptor, manifests, callbacks = self.artifact_factory(
                    self.key, value, lambda artifact: self._run_table_descriptor(value, artifact))
                if hasattr(value, "evaluation_roles"):
                    descriptor = {**descriptor, **value.evaluation_metadata()}
                self.artifacts.extend(manifests)
                self.commits.extend(callbacks)
                cached = (descriptor, self.commits[first_commit:], self.artifacts[first_artifact:])
                self.table_cache[cached_key] = cached
                self.prepared_tables[cached_key] = cached
            else:
                descriptor = self._with_artifact_reference(value, self._table(value))
            if hasattr(value, "evaluation_roles"):
                descriptor = {**descriptor, **value.evaluation_metadata()}
            return {**descriptor, **getattr(value, "_partition_info", {})}
        if isinstance(value, Histogram):
            return {"_type": "histogram", "values": self._json_fields(value.histogram), "bins": self._json_fields(value.bins)}
        if isinstance(value, Graph):
            value._prepare_file()
            # Public Graph binding owns a separate file lifecycle. Every native
            # observation must stage its current encoded bytes, not that possibly
            # older bound path; this also preserves independent live captures.
            ref = self.stage_file(None, data=value.data, name="graph.graph.json")
            return self._with_artifact_reference(value,
                {**ref, "_type": value.kind, "caption": value.caption,
                 **self._json_fields(value.metadata)})
        if isinstance(value, Plotly):
            from .plotly_resources import encode_resources
            resources = encode_resources(value, self.stage_file)
            return {"_type": "plotly", "spec": self._json_fields(value.spec),
                    **({"resources": resources} if resources else {})}
        if isinstance(value, Media):
            if hasattr(value, "_reference_uri"):
                from .media_references import descriptor
                owner = self._schema_artifact if self.context in {"artifact", "table"} else None
                ref = descriptor(value, owner, history=self.context == "table")
            elif self.context in {"history", "value"} and id(value) in self.media_cache:
                prepared = self.media_cache[id(value)]
                ref, callback, _ = prepared
                self.commits.append(callback)
                self.prepared_media[id(value)] = prepared
            elif self.stage_media is not None and self.context in {"history", "value"} and value.path is not None:
                ref = self.stage_media(value)
            else:
                ref = self.stage_file(value.path, data=value.data, name=value.path.name if value.path else value.kind)
            mime = value.metadata.get("mime_type") or (mimetypes.guess_type(str(value.path))[0] if value.path else None)
            return self._with_artifact_reference(value,
                {**ref, "_type": value.kind, "caption": value.caption, "mime_type": mime,
                 **self._json_fields(value.metadata)})
        if hasattr(value, "_backprop_plot"):
            from .legacy_charts import Visualize, rendering_chart
            if isinstance(value, Visualize):
                value = rendering_chart(value, self.run)
            if value.kind == "vega":
                from .custom_charts import substitute
                return {"_type": "vega", "spec": substitute(value.spec.custom_spec, value.spec.fields, value.spec.string_fields),
                        "table": self.encode(value.table), "title": value.title, "spec_name": value.spec.spec_name}
            chart_key = ("Custom Chart Tables/" if value.spec.split_table else "") + self.key + "_table"
            return {"_type": "plot", "kind": value.kind, "table": self._at_key(chart_key, value.table),
                    "fields": value.fields, "title": value.title,
                    "spec_name": value.spec.spec_name, "spec_fields": value.spec.fields, "string_fields": value.spec.string_fields,
                    "split_table": value.spec.split_table}
        if isinstance(value, WBValue):
            owner = self._schema_artifact if self._schema_artifact is not None else self.run
            payload = value.to_json(owner)
            if not isinstance(payload, dict):
                raise TypeError("WBValue.to_json must return a dictionary")
            return self._json_fields(payload)
        # Pinned val_to_json dispatches raw plotting-library objects before
        # ordinary JSON conversion. Restrict this implicit path to recognized
        # library names; an unrelated object's to_json is not a logging API.
        typename = type(value).__module__ + "." + type(value).__name__
        is_plotly = typename.startswith("plotly.")
        if _is_matplotlib(value) or is_plotly:
            if is_plotly and not typename.endswith(".Figure"):
                raise ValueError("Logged Plotly objects must be Figure instances")
            return self.encode(Plotly.make_plot_media(value))
        if isinstance(value, dict):
            if self.context in {"table", "artifact"}:
                from .json_keys import encode_mapping
                return encode_mapping(value, self.encode)
            if any(not isinstance(key, str) for key in value):
                raise TypeError("Logged dictionary keys must be strings")
            return {key: self._at_key(key, item) for key, item in value.items()}
        if self._preserve_sequences and self.context in {"table", "artifact"} and type(value) in {tuple, set, frozenset}:
            from .sequence_codec import encode_sequence
            return encode_sequence(value, self.encode)
        if isinstance(value, (list, tuple)):
            if (self.stage_media_batch is not None and self.context in {"history", "value"} and value
                    and isinstance(value[0], (Image, Audio, Video, Html))
                    and all(isinstance(item, type(value[0])) for item in value)):
                return self._media_batch(value)
            return [self.encode(item) for item in value]
        if hasattr(value, "detach") and hasattr(value, "cpu"):
            value = value.detach().cpu().numpy()
        if hasattr(value, "tolist"):
            if self.context in {"table", "artifact"} and getattr(value, "ndim", 0) > 0:
                return {"_type": "ndarray", "data": self.encode(value.tolist()),
                        "shape": list(value.shape), "dtype": str(value.dtype)}
            if self.context == "history" and getattr(value, "size", 0) > 32:
                return self.encode(Histogram(value, num_bins=32))
            return self.encode(value.tolist())
        if hasattr(value, "item"):
            return self.encode(value.item())
        raise TypeError(f"Unsupported logged value: {type(value).__name__}")

    def _json_fields(self, value):
        """Keep rich-object metadata in its declared JSON representation.

        Main callers: Histogram, Media, BoundingBoxes2D and Plotly encoding.
        Tuple identity belongs to user Table cells; chart coordinates, histogram
        bins and box positions remain JSON arrays expected by their viewers.
        """
        previous, self._preserve_sequences = self._preserve_sequences, False
        try:
            return self.encode(value)
        finally:
            self._preserve_sequences = previous

    def _media_batch(self, values):
        """Prepare the fresh per-log bindings used by homogeneous media lists.

        Main callers: encode. Pinned val_to_json rebinds every BatchableMedia
        list on every log; duplicate references serialize the object's final
        binding. Native lists retain that shared identity without filename
        collisions. Failed rows never replace a prior committed scalar binding.
        """
        prepared_here = set()
        for media in values:
            if media.path is not None and id(media) not in prepared_here:
                prepared = self.stage_media_batch(media)
                self.media_cache[id(media)] = prepared
                prepared_here.add(id(media))
        return [self.encode(media) for media in values]

    def _at_key(self, key, value):
        previous, self.key = self.key, key
        try:
            return self.encode(value)
        finally:
            self.key = previous

    def _with_artifact_reference(self, value, descriptor):
        """Attach provenance only at a run boundary, never inside artifact JSON."""
        if self.context in {"history", "value"}:
            from .offline_artifacts import artifact_reference
            reference = artifact_reference(value, self.settings)
            if reference is not None:
                return {**descriptor, "artifact_path": reference}
        return descriptor

    def _run_table_descriptor(self, table, artifact):
        """Serialize history schemas with their actual implicit artifact owner.

        Main callers: prepare_table's descriptor callback. Custom types may
        store constraints as artifact files; creating the real owner first lets
        their serializers add those files before the manifest is finalized.
        Related native tables retain history table IDs instead of file paths.
        """
        previous, self._schema_artifact = self._schema_artifact, artifact
        try:
            return self._with_artifact_reference(table, self._table(table))
        finally:
            self._schema_artifact = previous

    def _table(self, table):
        if id(table) in self._encoding_tables:
            raise ValueError("Table relationships cannot form a cycle")
        self._encoding_tables.add(id(table))
        try:
            return self._encode_table(table)
        finally:
            self._encoding_tables.remove(id(table))

    def _encode_table(self, table):
        run_value = self.context in {"history", "value"}
        if run_value and table.log_mode == "IMMUTABLE" and table._snapshot is not None:
            if len(table.data) != table._offset or table._generation != table._snapshot_generation:
                warnings.warn("Immutable Table was changed after logging; first snapshot retained", RuntimeWarning, stacklevel=3)
            return table._snapshot
        if (run_value and table._snapshot is not None and table._generation == table._snapshot_generation
                and table._offset == len(table.data)):
            return table._snapshot
        offset = table._offset if run_value and table.log_mode == "INCREMENTAL" else 0
        count = len(table.data)
        generation = table._generation
        if count - offset > table.MAX_ARTIFACT_ROWS:
            if getattr(self.settings, "table_raise_on_max_row_limit_exceeded", False) or getattr(self.settings, "strict", False):
                raise ValueError(f"Table log exceeds {table.MAX_ARTIFACT_ROWS} rows; log smaller incremental batches")
            warnings.warn(f"Truncating Table snapshot to {table.MAX_ARTIFACT_ROWS} rows", RuntimeWarning, stacklevel=3)
            count = offset + table.MAX_ARTIFACT_ROWS
        previous_context = self.context
        try:
            self.context = "table"
            data = self.encode(table.data[offset:count])
        finally:
            self.context = previous_context
        from .json_keys import table_schema_fields
        result = {"_type": "table", "table_id": table._id, "columns": list(table.columns),
                  "data": data, "log_mode": table.log_mode, "row_offset": offset + table._base_offset,
                  **table_schema_fields([typ.to_json(self._schema_artifact, _relationship_ids=True)
                                         for typ in table._types], for_resume=run_value)}
        # Relationship dependencies belong to their parent snapshot. They do
        # not independently log artifacts or consume their own history cursors.
        try:
            self.context = "table"
            from .table_native_schema import relationship_targets
            related = {target._id: self.encode(target) for target in relationship_targets(table._types)}
        finally:
            self.context = previous_context
        if related:
            result["related_tables"] = related
        from .offline_artifacts import legacy_table_view
        result = legacy_table_view(table, result, self.settings, self.context)
        def commit():
            table._offset = count
            table._snapshot = result
            table._snapshot_generation = generation
        if run_value:
            self.commits.append(commit)
        return result

    def commit(self):
        completed = set()
        for operation in self.commits:
            if id(operation) not in completed:
                operation()
                completed.add(id(operation))


# Values are fully defined now, so native dtype registration cannot create the
# Table -> media -> Table import cycle. Public registry lookups work immediately.
from .table_native_types import register_native_types as _register_native_types
_register_native_types()

# Public data-type import aliases share the same implementation classes.
from .extended_tables import JoinedTable, EvalTable
