"""Integration contracts for read-only queries over real SDK-generated events.

Architecture: logging Run -> durable journal -> MemorySink -> Public Api ->
assert reconstructed behavior and downloaded bytes. Additional hostile references
exercise storage integrity and destination confinement. No live credentials.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timezone
import csv
import hashlib
import io
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api, RunNotFoundError
from backprop.apis.public.files import File
from backprop.apis.public.query import matches
from backprop.foundry import FoundryError, FoundryStore
from backprop.errors import CommError
from backprop.transport import BackpropError, MemorySink


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sink = MemorySink()
        self.api = Api._from_store(self.sink, entity="team", project="experiments", config={"app_url": "https://app.example"})
        self.active = []

    def tearDown(self):
        for run in self.active:
            if not run._closed: run.finish()
        self.temp.cleanup()

    def run_log(self, id="one", **kwargs):
        values = {"id": id, "project": "experiments", "entity": "team", "dir": self.temp.name,
                  "_sink": self.sink, "_deployment": {"app_url": "https://app.example"}, **kwargs}
        run = backprop.Run(**values)
        self.active.append(run)
        return run

    def test_live_summary_metadata_nested_mutations_and_read_only_boundary(self):
        writer = self.run_log(config={"optimizer": {"lr": .01}})
        writer.define_metric("loss", summary="min,mean")
        writer.log({"loss": 4, "batch": {"n": 2}})
        writer.log({"loss": 2})
        writer.name = "renamed"
        writer.summary["eval"] = {"accuracy": .2}
        writer.summary["eval"]["accuracy"] = .9
        writer.flush()
        run = self.api.run("one")
        self.assertEqual(run.name, "renamed")
        self.assertEqual(run.summary["loss"], {"min": 2, "mean": 3})
        self.assertEqual(run.summary.eval.accuracy, .9)
        self.assertEqual(run.config, {"optimizer": {"lr": .01}})
        self.assertEqual(run.state, "running")
        count = len(self.sink.events)
        run.summary.eval.accuracy = .1
        self.assertEqual(self.api.run("one").summary.eval.accuracy, .1)
        with self.assertRaises(NotImplementedError): run.delete()
        self.assertEqual(len(self.sink.events), count + 1)

    def test_resume_dedup_and_archive_order_preserve_full_history(self):
        first = self.run_log()
        first.log({"loss": 4})
        first.finish()
        initial_time = self.api.run("one").created_at
        second = self.run_log(resume="must")
        second.log({"loss": 2})
        second.finish()
        self.sink.events += copy.deepcopy(self.sink.events[:2])
        self.api.flush()
        run = self.api.run("team/experiments/one")
        self.assertEqual(run.created_at, initial_time)
        self.assertEqual([row["loss"] for row in run.scan_history()], [4, 2])
        self.assertEqual(run.lastHistoryStep, 1)

    def test_scan_history_is_complete_with_sparse_keys_and_exclusive_end(self):
        writer = self.run_log()
        writer.log({"a": 0, "b": 5})
        writer.log({"a": 1})
        writer.log({"a": 2, "b": 7})
        writer.finish()
        run = self.api.run("one")
        self.assertEqual(list(run.scan_history(keys=["a", "b"], page_size=1)), [{"a": 0, "b": 5}, {"a": 2, "b": 7}])
        self.assertEqual([row["_step"] for row in run.scan_history(min_step=1, max_step=2)], [1])
        self.assertEqual(run.scan_history().max_step, 2)
        with self.assertRaises(CommError): list(run.scan_history(keys="a"))
        with self.assertRaises(CommError): list(run.scan_history(page_size=0))

    def test_sampled_history_and_combined_histories_keep_run_identity(self):
        for name in ("one", "two"):
            writer = self.run_log(name)
            for i in range(11): writer.log({"loss": i, "epoch": i * 2})
            writer.finish()
        sampled = self.api.run("one").history(samples=3, keys=["loss"], x_axis="epoch", pandas=False)
        self.assertEqual(sampled, [{"epoch": 0, "loss": 0}, {"epoch": 10, "loss": 5}, {"epoch": 20, "loss": 10}])
        rows = self.api.runs().histories(samples=2)
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["run_id"] for row in rows}, {"one", "two"})

    def test_cached_reader_discovers_new_live_rows_in_default_scan(self):
        writer = self.run_log()
        writer.log({"loss": 3}); writer.flush()
        reader = self.api.run("one")
        self.assertEqual(reader.lastHistoryStep, 0)
        writer.log({"loss": 2}); writer.flush()
        self.assertEqual([row["loss"] for row in reader.scan_history()], [3, 2])
        self.assertEqual(reader.lastHistoryStep, 1)

    def test_filters_order_and_project_entity_isolation(self):
        for name, loss, group in [("one", 3, "a"), ("two", 1, "b"), ("three", 2, "a")]:
            writer = self.run_log(name, config={"optimizer": {"lr": loss / 10}}, group=group, tags=["baseline", group])
            writer.log({"loss": loss}); writer.finish()
        other = self.run_log("one", entity="other")
        other.log({"loss": -100}); other.finish()
        runs = self.api.runs(filters={"$and": [{"config.optimizer.lr": {"$gte": .2}}, {"tags": {"$in": ["a"]}}]}, order="-summary_metrics.loss", per_page=1)
        self.assertEqual([run.id for run in runs], ["one", "three"])
        self.assertEqual([project.path for project in self.api.projects()], [["team", "experiments"]])
        self.assertEqual(self.api.project("experiments").path, ["team", "experiments"])
        self.assertEqual([run.id for run in self.api.runs(order="+config.optimizer.lr.value")], ["two", "three", "one"])

    def test_invalid_query_rejected_even_with_empty_or_nonmatching_data(self):
        with self.assertRaises(ValueError): self.api.runs(filters={"state": {"$unknown": True}})
        with self.assertRaises(ValueError): self.api.runs(filters={"$or": [{"name": "x"}, {"name": {"$bad": 2}}]})
        with self.assertRaises(ValueError): self.api.runs(order="-")
        with self.assertRaises(ValueError): self.api.runs(per_page=0)
        with self.assertRaises(ValueError): self.api.runs(filters={"name": {"$regex": "(?<=a)b"}})

    def test_query_operators_match_documented_semantics(self):
        record = {"x": 3, "tags": ["a", "b"], "label": "train-1", "nullable": None}
        cases = [({"x": {"$eq": 3, "$ne": 4, "$gt": 2, "$gte": 3, "$lt": 4, "$lte": 3}}, True),
                 ({"$or": [{"x": 2}, {"x": 3}]}, True), ({"$nor": [{"x": 1}, {"x": 2}]}, True),
                 ({"tags": {"$in": ["b"], "$nin": ["c"]}}, True), ({"tags": "a"}, True),
                 ({"missing": {"$exists": False}, "nullable": {"$exists": True}}, True),
                 ({"label": {"$regex": "^train-[0-9]+$"}}, True), ({"x": {"$gt": 4}}, False)]
        for query, expected in cases:
            with self.subTest(query=query): self.assertEqual(matches(record, query), expected)

    def test_regex_strict_end_anchor_and_explicit_full_dialect_dependency(self):
        self.assertTrue(matches({"name": "train-1"}, {"name": {"$regex": "^train-[0-9]+$"}}))
        self.assertFalse(matches({"name": "train-1\n"}, {"name": {"$regex": "^train-[0-9]+$"}}))
        self.assertTrue(matches({"name": "$"}, {"name": {"$regex": "[$]"}}))
        if importlib.util.find_spec("re2"):
            self.assertTrue(matches({"name": "α"}, {"name": {"$regex": r"^\p{Greek}+$"}}))
        else:
            with self.assertRaisesRegex(ImportError, "google-re2"):
                matches({"name": "α"}, {"name": {"$regex": r"^\p{Greek}+$"}})

    def test_file_snapshot_retrieval_sql_like_and_traversal(self):
        writer = self.run_log()
        source = Path(self.temp.name) / "report.txt"
        source.write_text("original")
        writer.save(str(source), base_path=self.temp.name, policy="now")
        source.write_text("changed")
        writer.finish()
        run = self.api.run("one")
        self.assertEqual([file.name for file in run.files(pattern="%.txt")], ["report.txt"])
        with self.assertRaises(CommError): run.files(names=["report.txt"], pattern="%.txt")
        root = Path(self.temp.name) / "download"
        with run.file("report.txt").download(root) as stream: self.assertEqual(stream.read(), "original")
        with self.assertRaises(CommError): run.file("report.txt").download(root)
        with run.file("report.txt").download(root, exist_ok=True) as stream: self.assertEqual(stream.read(), "original")
        hostile = File(self.sink, "../outside", run.file("report.txt")._reference)
        with self.assertRaises(CommError): hostile.download(root)

    def test_corrupt_file_cannot_overwrite_known_good_destination(self):
        writer = self.run_log()
        source = Path(self.temp.name) / "weights.bin"
        source.write_bytes(b"valid")
        writer.save(str(source), base_path=self.temp.name, policy="now"); writer.finish()
        file = self.api.run("one").file("weights.bin")
        root = Path(self.temp.name) / "download"
        with file.download(root): pass
        self.sink.files[file._reference["path"]] = b"invalid"
        with self.assertRaises(BackpropError): file.download(root, replace=True)
        self.assertEqual((root / "weights.bin").read_bytes(), b"valid")

    def test_nonfinite_metrics_roundtrip_without_becoming_null(self):
        writer = self.run_log()
        writer.log({"nan": float("nan"), "positive": float("inf"), "negative": float("-inf")})
        writer.finish()
        row = self.api.run("one").history(pandas=False)[0]
        self.assertTrue(math.isnan(row["nan"]))
        self.assertEqual(row["positive"], float("inf")); self.assertEqual(row["negative"], float("-inf"))

    def test_large_table_is_hydrated_and_integrity_verified(self):
        writer = self.run_log()
        rows = [[i, "x" * 100] for i in range(1800)]
        writer.log({"table": backprop.Table(columns=["i", "text"], data=rows)})
        writer.finish()
        reference = next(event for event in self.sink.events if event["type"] == "history")["payload"]["metrics"]["table"]
        self.assertTrue(reference["external"])
        self.assertEqual(self.api.run("one").history(pandas=False)[0]["table"]["data"], rows)
        self.sink.files[reference["path"]] = b"corrupted"
        self.api.flush()
        with self.assertRaises(BackpropError): self.api.run("one").history(pandas=False)

    def test_artifact_roundtrip_collection_cursor_and_type_validation(self):
        writer = self.run_log()
        source = Path(self.temp.name) / "model.bin"
        versions = []
        for i in range(3):
            source.write_bytes(f"weights-{i}".encode())
            artifact = backprop.Artifact("model", "weights", metadata={"generation": i})
            artifact.add_file(source)
            writer.log_artifact(artifact)
            versions.append(artifact.version)
        writer.finish()
        artifacts = self.api.artifacts("weights", "model", order="+created_at", per_page=1)
        first = next(iter(artifacts)); cursor = artifacts.cursor
        remainder = self.api.artifacts("weights", "model", order="+created_at", per_page=1, start=cursor)
        self.assertEqual([artifact.version for artifact in remainder], versions[1:])
        fetched = self.api.artifact(f"model:{first.version}", type="weights")
        root = Path(self.temp.name) / "artifacts"
        self.assertEqual(Path(fetched.get_path("model.bin").download(root)).read_bytes(), b"weights-0")
        self.assertEqual(self.api.artifact_type("weights").name, "weights")
        self.assertEqual(self.api.artifact_collection("weights", "model").name, "model")
        self.assertEqual([value.name for value in self.api.project("experiments").collections()], ["model"])
        with self.assertRaises(CommError): self.api.artifact(f"model:{first.version}", type="wrong")
        with self.assertRaises(ValueError): list(self.api.artifacts("weights", "different", start=cursor))
        self.assertFalse(self.api.artifact_exists("model:does-not-exist"))
        self.assertTrue(self.api.artifact_collection_exists("model", "weights"))

    def test_scalar_projection_does_not_download_unrequested_rich_values(self):
        writer = self.run_log()
        writer.log({"loss": 1})
        writer.log({"loss": 2, "table": backprop.Table(columns=["text"], data=[["x" * 150_000]])})
        writer.finish()
        with patch.object(self.sink, "download_file", side_effect=AssertionError("Unrequested download")):
            reader = self.api.run("one")
            self.assertEqual(reader.summary.loss, 2)
            self.assertEqual(list(reader.scan_history(keys=["loss"], max_step=1)), [{"loss": 1}])
            self.assertEqual(list(reader.scan_history(keys=["loss"])), [{"loss": 1}, {"loss": 2}])
            self.assertEqual(reader.history(keys=["loss"], pandas=False)[1]["loss"], 2)
            with self.assertRaisesRegex(CommError, "Unrequested download"):
                list(reader.scan_history(keys=["table"]))

    def test_console_lines_paths_and_refresh_cache(self):
        writer = self.run_log()
        writer.log_console("first\nsecond\n", "stdout")
        writer.log_console("third\n", "stderr")
        writer.flush()
        run = self.api.run("one")
        self.assertEqual([line.content for line in run.console_logs(last=2)], ["second", "third"])
        self.assertEqual(list(run.console_logs())[-1].level, "error")
        self.assertEqual(self.api.from_path(run.url).id, "one")
        self.assertEqual(self.api.from_path("team/experiments").name, "experiments")
        self.assertEqual(self.api.from_path("experiments").name, "experiments")
        writer.name = "new-name"; writer.flush()
        self.assertNotEqual(run.name, "new-name")
        run.load(force=True); self.assertEqual(run.name, "new-name")
        with self.assertRaises(CommError) as caught: self.api.run("missing")
        self.assertIsInstance(caught.exception.exc, RunNotFoundError)

    def test_console_write_fragments_are_joined_and_live_tail_refreshes(self):
        writer = self.run_log()
        writer.log_console("hel"); writer.log_console("lo"); writer.log_console("\n")
        writer.flush()
        reader = self.api.run("one")
        self.assertEqual([line.content for line in reader.console_logs()], ["hello"])
        writer.log_console("next\n"); writer.flush()
        self.assertEqual([line.content for line in reader.console_logs(last=1)], ["next"])

    def test_auto_media_files_preserve_versions_and_nested_references(self):
        writer = self.run_log()
        writer.log({"html": backprop.Html("<p>first</p>")})
        writer.log({"html": backprop.Html("<p>second</p>")})
        writer.finish()
        files = list(self.api.run("one").files())
        html_files = [file for file in files if file.mimetype == "text/html"]
        self.assertEqual(len(html_files), 2)
        self.assertEqual(len({file.name for file in html_files}), 2)
        from backprop.apis.public.files import file_references
        nested = {"path": "image", "sha256": "first", "size": 1, "masks": {
            "layer": {"path": "mask", "sha256": "second", "size": 2}}}
        self.assertEqual([value["path"] for value in file_references(nested)], ["image", "mask"])

    def test_new_file_and_artifact_collections_refresh_an_existing_reader(self):
        writer = self.run_log()
        reader = self.api.run("one")
        old_files = list(reader.files())
        source = Path(self.temp.name) / "new.txt"
        source.write_text("new")
        writer.save(source, base_path=self.temp.name, policy="now")
        artifact = backprop.Artifact("new", "file")
        artifact.add_file(source)
        writer.log_artifact(artifact)
        writer.flush()
        self.assertNotIn("new.txt", [file.name for file in old_files])
        self.assertIn("new.txt", [file.name for file in reader.files()])
        self.assertEqual([value.name.split(":")[0] for value in reader.logged_artifacts()], ["new"])

    def test_run_path_forms_and_environment_metadata_match_documented_reads(self):
        writer = self.run_log()
        writer.finish()
        for path in ["team/experiments/runs/one", "team/experiments:one", "experiments:one",
                     "experiments/one", " /team/experiments/one/ ", "/runs/one", "one"]:
            with self.subTest(path=path): self.assertEqual(self.api.run(path).id, "one")
        metadata = self.api.run("one").metadata
        self.assertIn("python", metadata)
        self.assertIn("os", metadata)
        self.assertNotIn("environment", metadata)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency unavailable")
    def test_summary_array_loads_lazily_from_verified_npy(self):
        import numpy as np
        writer = self.run_log()
        original = np.arange(40).reshape(5, 8)
        writer.summary["weights"] = original
        writer.finish()
        reader = self.api.run("one")
        self.assertEqual(reader.summary_metrics["weights"]["_type"], "numpy.ndarray")
        np.testing.assert_array_equal(reader.summary.weights, original)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency unavailable")
    def test_ndarray_table_cells_restore_exact_dtype_shape_and_nonfinite(self):
        import numpy as np
        writer = self.run_log()
        original = np.array([[1, np.inf], [2, np.nan]], dtype="float32")
        writer.log({"table": backprop.Table(columns=["array", "caption"], data=[[original, "x" * 140_000]])})
        writer.finish()
        reader = self.api.run("one")
        with patch.dict(sys.modules, {"numpy": None}):
            self.assertTrue(any(file.name.endswith("rich-value.json") for file in reader.files()))
        cell = reader.history(pandas=False)[0]["table"]["data"][0][0]
        self.assertEqual(cell.dtype, original.dtype)
        self.assertEqual(cell.shape, original.shape)
        np.testing.assert_array_equal(cell, original)
        if importlib.util.find_spec("pyarrow"):
            import pyarrow.parquet as pq
            result = self.api.run("one").download_history_exports(Path(self.temp.name) / "array-parquet")
            exported = pq.read_table(result.paths[0])
            self.assertIn("table", json.loads(exported.schema.metadata[b"backprop.json_columns"]))
            encoded = json.loads(exported.to_pylist()[0]["table"])["data"][0][0]
            self.assertEqual(encoded["shape"], [2, 2])
            self.assertEqual(encoded["dtype"], "float32")

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency unavailable")
    def test_empty_multidimensional_arrays_preserve_lost_trailing_axes(self):
        import numpy as np
        from backprop.apis.public.replay import hydrate
        for index, shape in enumerate([(0, 3), (2, 0, 3), (0, 2, 0, 3)]):
            writer = self.run_log(str(index))
            writer.log({"table": backprop.Table(columns=["array"], data=[[np.empty(shape, dtype="float32")]])})
            writer.finish()
            cell = self.api.run(str(index)).history(pandas=False)[0]["table"]["data"][0][0]
            self.assertEqual(cell.shape, shape)
            self.assertEqual(cell.dtype, np.dtype("float32"))
        for shape in [[-1, 0], [True], [2, 0, 3]]:
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                hydrate({"_type": "ndarray", "data": [], "dtype": "float32", "shape": shape}, self.sink)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency unavailable")
    def test_object_ndarray_cells_keep_list_values_at_declared_depth(self):
        import numpy as np
        original = np.empty(2, dtype=object)
        original[0], original[1] = [1, 2], [3, 4]
        writer = self.run_log()
        writer.log({"table": backprop.Table(columns=["array"], data=[[original]])})
        writer.finish()
        cell = self.api.run("one").history(pandas=False)[0]["table"]["data"][0][0]
        self.assertEqual(cell.shape, (2,))
        self.assertEqual(cell.dtype, np.dtype(object))
        self.assertEqual(cell[0], [1, 2])
        self.assertEqual(cell[1], [3, 4])

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "PyArrow optional dependency unavailable")
    def test_history_export_is_readable_parquet_with_sparse_rows(self):
        import pyarrow.parquet as pq
        writer = self.run_log()
        writer.log({"loss": 3})
        writer.log({"loss": 2, "accuracy": .5})
        writer.finish()
        result = self.api.run("one").download_history_exports(Path(self.temp.name) / "parquet")
        self.assertFalse(result.contains_live_data)
        self.assertEqual(result.errors, {})
        rows = pq.read_table(result.paths[0]).to_pylist()
        self.assertEqual([row["loss"] for row in rows], [3, 2])
        self.assertIsNone(rows[0]["accuracy"])

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "PyArrow optional dependency unavailable")
    def test_history_export_preserves_mixed_metric_types_and_empty_objects(self):
        import pyarrow.parquet as pq
        writer = self.run_log()
        writer.log({"mixed": 3})
        writer.log({"mixed": "3"})
        writer.log({"mixed": [1, float("inf")]})
        writer.finish()
        result = self.api.run("one").download_history_exports(Path(self.temp.name) / "mixed-parquet")
        table = pq.read_table(result.paths[0])
        self.assertIn("mixed", json.loads(table.schema.metadata[b"backprop.json_columns"]))
        values = [json.loads(row["mixed"]) for row in table.to_pylist()]
        self.assertEqual(values, [3, "3", [1, {"_type": "number", "value": "Infinity"}]])
        from backprop.apis.public.history import parquet_table
        import pyarrow as pa
        self.assertEqual(parquet_table([{"empty": {}}], pa).to_pylist(), [{"empty": "{}"}])

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "PyArrow optional dependency unavailable")
    def test_history_export_keeps_nested_empty_structs_and_unbounded_integers(self):
        import pyarrow.parquet as pq
        writer = self.run_log()
        writer.log({"objects": [{}], "integer": 2**100})
        writer.finish()
        result = self.api.run("one").download_history_exports(Path(self.temp.name) / "edge-parquet")
        table = pq.read_table(result.paths[0])
        self.assertEqual(set(json.loads(table.schema.metadata[b"backprop.json_columns"])), {"objects", "integer"})
        row = table.to_pylist()[0]
        self.assertEqual(json.loads(row["objects"]), [{}])
        self.assertEqual(json.loads(row["integer"]), 2**100)

    def test_external_table_media_and_summary_arrays_are_enumerated(self):
        import base64
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0HsAAAAASUVORK5CYII=")
        writer = self.run_log()
        writer.log({"samples": backprop.Table(columns=["prompt", "image"], data=[["x" * 150_000, backprop.Image(png, file_type="png")]])})
        writer.finish()
        reader = self.api.run("one")
        files = list(reader.files())
        self.assertTrue(any(file.mimetype == "image/png" for file in files))
        self.assertTrue(any(file.name.endswith("rich-value.json") for file in files))
        image = next(file for file in files if file.mimetype == "image/png")
        self.assertEqual(self.sink.download_file(image._reference), png)

    def test_console_tail_edits_use_same_stream_unicode_characters(self):
        writer = self.run_log()
        writer.log_console("progress: 🧠 0%")
        writer.log_console("warning\n", stream="stderr")
        writer._emit("console", {"stream": "stdout", "replace_tail": 4, "text": "✓ 100%\n"})
        writer.finish()
        lines = list(self.api.run("one").console_logs())
        self.assertEqual([line.content for line in lines], ["progress: ✓ 100%", "warning"])
        self.assertEqual(lines[1].level, "error")

    def test_table_timestamps_are_lossless_date_and_datetime_values(self):
        writer = self.run_log()
        day, moment = date(2026, 10, 3), datetime(2026, 10, 3, 12, 30, tzinfo=timezone.utc)
        writer.log({"dates": backprop.Table(columns=["date", "timestamp"], data=[[day, moment]])})
        writer.finish()
        table = self.api.run("one").history(pandas=False)[0]["dates"]
        self.assertEqual(table["data"], [[day, moment]])

    def test_history_key_metadata_needs_no_media_download(self):
        writer = self.run_log()
        writer.log({"loss": 3, "note": "ready"})
        writer.log({"loss": 2})
        writer.finish()
        reader = self.api.run("one")
        with patch.object(self.sink, "download_file", side_effect=AssertionError("No media read expected")):
            keys = reader.history_keys
        self.assertEqual(keys["keys"]["loss"], {"count": 2, "types": ["number"], "previousValue": 2})
        self.assertEqual(keys["lastStep"], 1)
        self.assertFalse(reader.read_only)
        self.assertFalse(reader.display())

    def test_host_filters_read_environment_metadata(self):
        writer = self.run_log(settings=backprop.Settings(host="training-host"))
        writer.finish()
        self.assertEqual([run.id for run in self.api.runs(filters={"host": "training-host"})], ["one"])


class NativeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.config = {"host": "https://foundry.example.com", "stream_dataset_rid": "ri.foundry.main.dataset.events",
                       "media_dataset_rid": "ri.foundry.main.dataset.files"}
        self.store = FoundryStore(self.config, "test-token")
        self.data = b'{"json":"must remain bytes"}'
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.ref = {"dataset_rid": self.config["media_dataset_rid"], "path": f"blobs/{self.digest}/report.json",
                    "sha256": self.digest, "size": len(self.data), "branch": "master"}

    def test_authenticated_download_is_raw_and_verified(self):
        with patch.object(self.store, "request", return_value=self.data) as request:
            self.assertEqual(self.store.download_file(self.ref), self.data)
            self.assertTrue(request.call_args.kwargs["raw"])
        with patch.object(self.store, "request", return_value=b"wrong"):
            with self.assertRaises(FoundryError): self.store.download_file(self.ref)

    def test_bad_reference_never_sends_credentials(self):
        with patch.object(self.store, "request") as request:
            for override in [{"dataset_rid": "other"}, {"branch": "other"}, {"path": "https://evil.example/x"},
                             {"path": f"blobs/{self.digest}/../secret"}]:
                with self.subTest(override=override), self.assertRaises(ValueError): self.store.download_file({**self.ref, **override})
            request.assert_not_called()

    def test_archive_keyset_pages_handle_equal_timestamps(self):
        self.store._archive_page_size = 2
        pages = [[{"timestamp": "same", "event_id": "c"}, {"timestamp": "same", "event_id": "b"}],
                 [{"timestamp": "same", "event_id": "a"}]]
        with patch.object(self.store, "_query_csv", side_effect=pages) as query:
            self.assertEqual([row["event_id"] for row in self.store.query_archive(project="x' OR 1=1")], ["c", "b", "a"])
            second_sql = query.call_args_list[1].args[0]
            self.assertIn("event_id <", second_sql)
            self.assertNotIn("x' OR 1=1", second_sql)
            self.assertIn("ORDER BY timestamp DESC, event_id DESC", second_sql)

    def test_legal_large_event_survives_csv_archive_reader(self):
        payload = json.dumps({"text": "x" * 140_000})
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["event_id", "payload"]); writer.writerow(["large", payload])
        with patch.object(self.store, "request", side_effect=[{"type": "succeeded", "queryId": "q"}, output.getvalue().encode()]):
            self.assertEqual(self.store._query_csv("SELECT scoped_event")[0]["payload"], payload)


if __name__ == "__main__": unittest.main()
