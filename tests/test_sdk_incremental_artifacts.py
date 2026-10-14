"""Verify full incremental reads without rewriting immutable delta files.

Architecture: real Table logs create a linked sequence -> Artifact.get combines
raw deltas -> verified files and per-increment media retain their original owner.
Fault injection checks explicit missing/corrupt/cyclic chain errors. Offline
tests prove local reconstruction works even when visible artifact URLs are off.
Main callers: unittest discovery; no service or optional dependencies required.
"""
import copy
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.artifact_events import iter_artifact_events
from backprop.object_codec import decode_object
from backprop.transport import BackpropError, MemorySink, read_journal, sync


def png(red, green, blue):
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, red, green, blue]))) + chunk(b"IEND", b""))


class IncrementalArtifactReads(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MemorySink()
        self.runs = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.api = backprop.Api._from_store(self.store, entity="owner", project="increments")

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        self.environment.stop()
        self.temporary.cleanup()

    def new(self, *, offline=False, allow=True, **kwargs):
        run = backprop.Run(project="increments", entity="owner", id="writer", dir=self.root,
            mode="offline" if offline else "online", settings={"console": "off", "silent": True,
            "disable_git": True, "allow_offline_artifacts": allow, "flush_interval": 3600},
            _sink=None if offline else self.store, _deployment={}, **kwargs)
        self.runs.append(run)
        return run

    def outputs(self):
        return [event["payload"] for event in iter_artifact_events(self.store.events)]

    def table_entry(self, artifact):
        return next(entry for name, entry in artifact.manifest.entries.items() if name.endswith(".table.json"))

    def chain(self):
        run = self.new()
        table = backprop.Table(columns=["x"], log_mode="INCREMENTAL")
        for value in range(3):
            table.add_data(value)
            run.log({"samples": table})
        run.finish()
        return self.outputs()

    def test_every_version_reads_exact_prefix_and_retains_raw_delta(self):
        manifests = self.chain()
        for index, manifest in enumerate(manifests):
            artifact = self.api.artifact(manifest["artifact_id"])
            entry = self.table_entry(artifact)
            self.assertEqual(json.loads(entry._read_verified_bytes())["data"], [[index]])
            with patch.object(self.store, "read_events", wraps=self.store.read_events) as reads:
                recovered = artifact.get(entry.path)
                self.assertEqual(recovered.data, [[number] for number in range(index + 1)])
                self.assertEqual(reads.call_count, int(index > 0))
                self.assertIs(artifact.get(entry.path), recovered)
            self.assertEqual(recovered._increment_num, index)
        resumed = self.new(resume="must")
        resumed.log({"samples": backprop.Table(columns=["x"], data=[[3]], log_mode="INCREMENTAL")})
        resumed.finish()
        latest = self.api.artifact(self.outputs()[-1]["artifact_id"])
        self.assertEqual(latest.get(self.table_entry(latest).path).data, [[0], [1], [2], [3]])

    def test_previous_media_decodes_from_its_own_artifact(self):
        run = self.new()
        table = backprop.Table(columns=["n", "image"], log_mode="INCREMENTAL")
        images = [png(255, 0, 0), png(0, 255, 0)]
        for number, data in enumerate(images):
            table.add_data(number, backprop.Image(data))
            run.log({"samples": table})
        run.finish()
        artifact = self.api.artifact(self.outputs()[-1]["artifact_id"])
        recovered = artifact.get(self.table_entry(artifact).path)
        self.assertEqual([row[0] for row in recovered.data], [0, 1])
        self.assertEqual([row[1]._bytes() for row in recovered.data], images)
        self.assertNotEqual(recovered.data[0][1]._artifact_source.id, recovered.data[1][1]._artifact_source.id)

    def test_increment_prefix_preserves_foreign_index_relationships(self):
        run = self.new()
        source = backprop.Table(columns=["value"], data=[[10], [20]])
        table = backprop.Table(columns=["index"], data=[[source.index_ref(0)]], log_mode="INCREMENTAL")
        run.log({"linked": table})
        table.add_data(source.index_ref(1))
        run.log({"linked": table})
        run.finish()
        artifact = self.api.artifact(self.outputs()[-1]["artifact_id"])
        name = next(name for name in artifact.manifest.entries
                    if name.endswith(".table.json") and not name.startswith("media/"))
        recovered = artifact.get(name)
        self.assertEqual(recovered.data, [[0], [1]])
        self.assertEqual(recovered._types[0].table.data, [[10], [20]])

    def test_offline_false_private_chain_recovers_full_data_before_and_after_sync(self):
        run = self.new(offline=True, allow=False)
        table = backprop.Table(columns=["x"], log_mode="INCREMENTAL")
        for number in range(3):
            table.add_data(number)
            run.log({"samples": table})
        latest = table._artifact_target
        entry = self.table_entry(latest)
        with patch("backprop.transport.native_store", side_effect=AssertionError("offline auth fallback")):
            self.assertEqual(latest.get(entry.path).data, [[0], [1], [2]])
        self.assertEqual(len(json.loads(entry._read_verified_bytes())["previous_increments_paths"]), 2)
        for event in read_journal(run.sync_dir):
            if event["type"] == "history":
                descriptor = event["payload"]["metrics"]["samples"]
                self.assertNotIn("artifact_path", descriptor)
                self.assertEqual(descriptor["previous_increments_paths"], [])
        run.finish()
        sync(run.sync_dir, sink=self.store)
        uploaded = self.api.artifact(self.outputs()[-1]["artifact_id"])
        self.assertEqual(uploaded.get(self.table_entry(uploaded).path).data, [[0], [1], [2]])

    def test_missing_or_corrupt_earlier_increment_never_returns_partial_table(self):
        manifests = self.chain()
        artifact = self.api.artifact(manifests[-1]["artifact_id"])
        first = next(event for event in self.store.events if event["type"] == "history")
        self.store.events.remove(first)
        with self.assertRaisesRegex(ValueError, "ID not found"):
            artifact.get(self.table_entry(artifact).path)
        self.store.events.insert(0, first)
        reference = manifests[0]["files"][0]
        self.store.files[reference["path"]] = b"changed bytes"
        with self.assertRaisesRegex(BackpropError, "SHA-256"):
            artifact.get(self.table_entry(artifact).path)

    def test_repeated_missing_and_cyclic_chain_metadata_is_rejected(self):
        manifests = self.chain()
        artifact = self.api.artifact(manifests[-1]["artifact_id"])
        entry = self.table_entry(artifact)
        value = json.loads(entry._read_verified_bytes())
        cases = []
        missing = copy.deepcopy(value)
        missing["previous_increments_paths"].pop()
        cases.append(missing)
        duplicate = copy.deepcopy(value)
        duplicate["previous_increments_paths"][1] = duplicate["previous_increments_paths"][0]
        cases.append(duplicate)
        cycle = copy.deepcopy(value)
        cycle["previous_increments_paths"][1] = entry.ref_url()
        cases.append(cycle)
        invalid = copy.deepcopy(value)
        invalid["increment_num"] = True
        cases.append(invalid)
        for malformed in cases:
            with self.subTest(value=malformed), self.assertRaises(ValueError):
                decode_object(malformed, artifact)

    def test_mismatched_schema_and_reordered_chain_fail_explicitly(self):
        manifests = self.chain()
        artifact = self.api.artifact(manifests[-1]["artifact_id"])
        value = json.loads(self.table_entry(artifact)._read_verified_bytes())
        changed = copy.deepcopy(value)
        changed["columns"] = ["different"]
        with self.assertRaisesRegex(ValueError, "columns changed"):
            decode_object(changed, artifact)
        reordered = copy.deepcopy(value)
        reordered["previous_increments_paths"].reverse()
        with self.assertRaisesRegex(ValueError, "out of order"):
            decode_object(reordered, artifact)


if __name__ == "__main__":
    unittest.main()
