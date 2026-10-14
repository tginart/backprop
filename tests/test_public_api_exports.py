"""Verify export fidelity using real logging events and an actual Parquet reader.

Architecture: SDK values -> MemorySink journal -> Public Api snapshot -> Parquet
-> existing tagged-value decoder. Scalar columns remain native Arrow columns;
Table containers and arrays retain the types needed by their saved constraints.
A separate changing-store fixture proves that completion and exported rows share
one read boundary. No W&B service behavior or Foundry network is simulated here.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api
from backprop.apis.public.history import IncompleteRunHistoryError
from backprop.apis.public.replay import hydrate


@unittest.skipUnless(importlib.util.find_spec("pyarrow"), "Parquet export requires optional PyArrow")
class HistoryExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sink = backprop.MemorySink()
        self.api = Api._from_store(self.sink, entity="team", project="exports")

    def writer(self, **kwargs):
        run = backprop.Run(id="one", entity="team", project="exports", dir=self.temp.name,
                           _sink=self.sink, settings={"silent": True, "console": "off"}, **kwargs)
        self.addCleanup(lambda: run.finish() if not run._closed else None)
        return run

    def export(self, reader=None, **kwargs):
        import pyarrow.parquet as pq
        reader = reader or self.api.run("one")
        result = reader.download_history_exports(Path(self.temp.name) / "parquet", **kwargs)
        return result, pq.read_table(result.paths[0])

    def test_nested_typed_table_cells_roundtrip_without_changing_scalar_columns(self):
        cells = [(1, "a", frozenset({2})), {3, (4, "b")}, frozenset({("c", 5)})]
        run = self.writer()
        run.log({"table": backprop.Table(columns=["tuple", "set", "frozen"], data=[cells],
                                         allow_mixed_types=True), "loss": 3})
        run.log({"loss": 2})
        run.finish()

        result, table = self.export()
        self.assertFalse(result.contains_live_data)
        self.assertEqual(result.errors, {})
        self.assertEqual(json.loads(table.schema.metadata[b"backprop.json_columns"]), ["table"])
        rows = table.to_pylist()
        self.assertEqual([row["loss"] for row in rows], [3, 2])
        self.assertIsNone(rows[1]["table"])
        restored = hydrate(json.loads(rows[0]["table"]), self.sink)["data"][0]
        self.assertEqual(restored, cells)
        self.assertEqual([type(cell) for cell in restored], [tuple, set, frozenset])
        self.assertIs(type(restored[0][2]), frozenset)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Array cells require optional NumPy")
    def test_array_cells_keep_dtype_empty_axes_and_object_container_identity(self):
        import numpy as np
        objects = np.empty(2, dtype=object)
        objects[0], objects[1] = (1, 2), frozenset({3, 4})
        cells = [np.array([1, 2], dtype="int16"), np.empty((2, 0, 3), dtype="float32"),
                 objects, np.array([float("inf"), float("nan")], dtype="float32")]
        run = self.writer()
        run.log({"table": backprop.Table(columns=["ints", "empty", "objects", "floats"],
                                         data=[cells], allow_mixed_types=True)})
        run.finish()

        _, table = self.export()
        self.assertIn("table", json.loads(table.schema.metadata[b"backprop.json_columns"]))
        restored = hydrate(json.loads(table.to_pylist()[0]["table"]), self.sink)["data"][0]
        for actual, expected in zip(restored, cells):
            self.assertEqual(actual.dtype, expected.dtype)
            self.assertEqual(actual.shape, expected.shape)
            np.testing.assert_equal(actual, expected)
        self.assertIs(type(restored[2][0]), tuple)
        self.assertIs(type(restored[2][1]), frozenset)

    def test_resume_between_reads_cannot_mix_live_rows_into_complete_snapshot(self):
        first = self.writer()
        first.log({"loss": 1})
        first.finish()
        complete = copy.deepcopy(self.sink.events)
        reader = self.api.run("one")
        resumed = self.writer(resume="must")
        resumed.log({"loss": 2})
        resumed.flush()
        live = copy.deepcopy(self.sink.events)

        with patch.object(self.sink, "read_events", side_effect=[complete, live]) as reads:
            result, table = self.export(reader)
        self.assertEqual(reads.call_count, 1)
        self.assertFalse(result.contains_live_data)
        self.assertEqual(table.column("loss").to_pylist(), [1])

    def test_live_snapshot_requires_explicit_permission_and_reports_live_flag(self):
        run = self.writer()
        run.log({"loss": 4})
        run.flush()
        reader = self.api.run("one")
        with self.assertRaises(IncompleteRunHistoryError):
            self.export(reader)
        self.assertFalse((Path(self.temp.name) / "parquet").exists())
        with patch.object(self.sink, "read_events", wraps=self.sink.read_events) as reads:
            result, table = self.export(reader, require_complete_history=False)
        self.assertEqual(reads.call_count, 1)
        self.assertTrue(result.contains_live_data)
        self.assertEqual(table.column("loss").to_pylist(), [4])


if __name__ == "__main__":
    unittest.main()
