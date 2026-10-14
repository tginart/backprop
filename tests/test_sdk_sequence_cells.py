"""Exercise container identity through real typed-value storage boundaries.

Architecture: encode nested Python containers -> immutable JSON -> artifact or
Public Api reconstruction. Separate checks keep ordinary history JSON behavior
unchanged and reject descriptors that attempt to name arbitrary constructors.
Main callers: unittest discovery; no optional library or network is required.
"""
import json
import math
import tempfile
import unittest

import backprop
from backprop.apis.public.replay import hydrate
from backprop.data_types import Encoder
from backprop.object_codec import decode_object
from backprop.sequence_codec import decode_sequence
from backprop.transport import sync


class SequenceCellTests(unittest.TestCase):
    def encode(self, value, context="table"):
        def no_file(*args, **kwargs):
            self.fail("Sequence cells must not stage files")
        return Encoder(no_file, context=context).encode(value)

    def test_nested_container_types_and_nonfinite_values_roundtrip(self):
        value = ("a", {3, "b", (1, 2)}, frozenset({("x", 4)}), float("inf"))
        encoded = json.loads(json.dumps(self.encode(value), allow_nan=False))
        for decoded in (decode_object(encoded, None), hydrate(encoded, None)):
            self.assertEqual(decoded, value)
            self.assertIs(type(decoded), tuple)
            self.assertIs(type(decoded[1]), set)
            self.assertIs(type(decoded[2]), frozenset)
            self.assertTrue(math.isinf(decoded[3]))

    def test_unordered_cells_have_stable_encoded_order(self):
        expected = self.encode({"z", 4, (2, "b"), frozenset({"x", "a"})})
        different_order = set()
        for item in (frozenset({"a", "x"}), (2, "b"), 4, "z"):
            different_order.add(item)
        self.assertEqual(self.encode(different_order), expected)
        strings = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in expected["data"]]
        self.assertEqual(strings, sorted(strings))

    def test_ordinary_history_keeps_json_tuple_and_rejects_sets(self):
        for context in ("history", "value"):
            self.assertEqual(self.encode((1, (2, 3)), context), [1, [2, 3]])
            with self.assertRaisesRegex(TypeError, "Unsupported logged value: set"):
                self.encode({1, 2}, context)

    def test_histogram_field_sequences_remain_json_arrays(self):
        histogram = backprop.Histogram(np_histogram=((2, 3), (0, 1, 2)))
        for context in ("table", "artifact"):
            encoded = self.encode((histogram, (4, 5)), context)
            self.assertEqual(encoded["data"][0], {"_type": "histogram", "values": [2, 3], "bins": [0, 1, 2]})
            self.assertEqual(encoded["data"][1]["kind"], "tuple")

    def test_malformed_or_unknown_constructors_fail_explicitly(self):
        for kind, data in (("list", []), ("os.system", []), ([], []), ("tuple", {}), ("set", None)):
            with self.subTest(kind=kind, data=data), self.assertRaises(ValueError):
                decode_sequence({"_type": "python.sequence", "kind": kind, "data": data}, lambda item: item)
        with self.assertRaises(TypeError):
            decode_sequence({"_type": "python.sequence", "kind": "set", "data": [[]]}, lambda item: item)

    def test_reference_only_hydration_retains_descriptors(self):
        encoded = self.encode((1, frozenset({2})))
        self.assertEqual(hydrate(encoded, None, external=False), encoded)

    def test_table_history_and_immutable_artifact_reconstruct_same_cells(self):
        cells = [(1, "a"), {2, 3}, frozenset({("b", 4)})]
        with tempfile.TemporaryDirectory() as directory:
            sink = backprop.MemorySink()
            table = backprop.Table(columns=["tuple", "set", "frozen"], data=[cells], allow_mixed_types=True)
            run = backprop.Run(project="sequence-cells", dir=directory, mode="offline",
                               settings={"console": "off", "silent": True, "disable_git": True})
            run.log({"cells": table})
            run.finish()
            sync(run.sync_dir, sink=sink)
            reader = backprop.Api._from_store(sink, project="sequence-cells").run(run.id)
            history = list(reader.scan_history())
            self.assertEqual(history[0]["cells"]["data"], [cells])
            artifact = list(reader.logged_artifacts())[0]
            entry = next(entry for entry in artifact.manifest.entries.values() if entry.path.endswith(".table.json"))
            restored = artifact.get(entry.path)
            self.assertEqual(restored.data, [cells])
            self.assertEqual([type(cell) for cell in restored.data[0]], [tuple, set, frozenset])


if __name__ == "__main__":
    unittest.main()
