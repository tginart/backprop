"""Native reference URLs round-trip through authenticated immutable manifests.

Architecture: publish a real Journal manifest to MemorySink -> share ref_url ->
resolve using the same configured store -> verify the destination's eager bytes.
Fault cases prove lookup never chooses an ambiguous ID, performs an offline
network fallback, or mutates the destination before a verified read succeeds.
Main callers: unittest discovery. No credentials or external services are used.
"""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.artifacts import Artifact
from backprop.transport import BackpropError, MemorySink


class NativeArtifactReferences(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sink = MemorySink()
        self.runs = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.settings = {"console": "off", "silent": True, "disable_git": True,
                         "flush_interval": 3600}

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        self.environment.stop()
        self.temporary.cleanup()

    def published(self, name="weights.bin"):
        path = self.root / "source.bin"
        path.write_bytes(b"original immutable bytes")
        source = Artifact("source", "model", metadata={"model": "test"})
        source.add_file(path, name)
        run = backprop.Run(project="reference-tests", entity="owner", id="source-run",
                           dir=self.root, settings=self.settings, _sink=self.sink, _deployment={})
        self.runs.append(run)
        run.log_artifact(source)
        run.finish()
        return source, path

    def target(self):
        target = Artifact("copy", "model")
        target._store = self.sink
        return target

    def test_returned_url_roundtrip_eager_copy_preserves_source_bytes(self):
        source, original = self.published()
        target = self.target()
        before_events = copy.deepcopy(self.sink.events)
        before_files = dict(self.sink.files)
        entry = target.add_reference(source.get_entry("weights.bin").ref_url())[0]
        self.assertEqual(entry.path, "weights.bin")
        self.assertEqual(entry._read_verified_bytes(), b"original immutable bytes")
        self.assertEqual(original.read_bytes(), b"original immutable bytes")
        self.assertEqual(self.sink.events, before_events)
        self.assertEqual(self.sink.files, before_files)
        original.write_bytes(b"producer changed its own file")
        self.sink.files.clear()
        self.assertEqual(entry._read_verified_bytes(), b"original immutable bytes")

    def test_reserved_and_unicode_paths_are_quoted_once_and_preserved(self):
        logical = "folder/weights ?#雪%2F.bin"
        source, _ = self.published(logical)
        uri = source.get_entry(logical).ref_url()
        self.assertIn("%3F%23%E9%9B%AA%252F", uri)
        self.assertEqual(urlsplit(uri).query, "")
        self.assertEqual(urlsplit(uri).fragment, "")
        entry = self.target().add_reference(uri)[0]
        self.assertEqual(entry.path, logical)
        renamed = self.target().add_reference(uri, name="renamed/#?.bin")[0]
        self.assertEqual(renamed.path, "renamed/#?.bin")
        self.assertEqual(renamed._read_verified_bytes(), b"original immutable bytes")

    def test_active_online_run_store_is_reused_without_default_auth(self):
        source, _ = self.published()
        active = backprop.Run(project="reference-tests", entity="owner", id="active",
                              dir=self.root, settings=self.settings, _sink=self.sink, _deployment={})
        self.runs.append(active)
        with patch.object(backprop, "run", active), patch("backprop.transport.native_store", side_effect=AssertionError("auth fallback")):
            entry = Artifact("copy", "model").add_reference(source.get_entry("weights.bin").ref_url())[0]
        self.assertEqual(entry._read_verified_bytes(), b"original immutable bytes")

    def test_standard_deployment_config_is_used_without_an_active_store(self):
        source, _ = self.published()
        config = {"host": "https://foundry.example", "stream_dataset_rid": "configured"}
        with patch.object(backprop, "run", None), patch("backprop.transport.load_config", return_value=config), patch("backprop.transport.native_store", return_value=self.sink) as native:
            entry = Artifact("copy", "model").add_reference(source.get_entry("weights.bin").ref_url())[0]
        native.assert_called_once_with(config)
        self.assertEqual(entry._read_verified_bytes(), b"original immutable bytes")

    def test_offline_or_missing_store_fails_without_falling_through_to_auth(self):
        source, _ = self.published()
        active = backprop.Run(project="reference-tests", id="offline", mode="offline",
                              dir=self.root, settings=self.settings, _deployment={})
        self.runs.append(active)
        uri = source.get_entry("weights.bin").ref_url()
        with patch.object(backprop, "run", active), patch("backprop.transport.native_store", side_effect=AssertionError("offline auth fallback")), patch.object(self.sink, "read_events", side_effect=AssertionError("offline read")):
            with self.assertRaisesRegex(RuntimeError, "offline or disabled"):
                Artifact("copy", "model").add_reference(uri)
        with patch.object(backprop, "run", None):
            with self.assertRaisesRegex(BackpropError, "BACKPROP_CONFIG"):
                Artifact("copy", "model").add_reference(uri)

    def test_missing_id_missing_entry_and_invalid_paths_leave_target_unchanged(self):
        source, _ = self.published()
        target = self.target()
        with self.assertRaisesRegex(ValueError, "ID not found"):
            target.add_reference("backprop-artifact://missing/weights.bin")
        with self.assertRaisesRegex(FileNotFoundError, "entry not found"):
            target.add_reference(f"backprop-artifact://{source.id}/absent.bin")
        for suffix in ("../weights.bin", "%2e%2e/weights.bin", "/absolute.bin", "weights.bin?query", "weights.bin#fragment", "weights.bin?", "weights.bin#", "%ZZ", "%FF"):
            with self.subTest(suffix=suffix), patch.object(self.sink, "read_events", side_effect=AssertionError("invalid reference read")):
                with self.assertRaises(ValueError):
                    target.add_reference(f"backprop-artifact://{source.id}/{suffix}")
        self.assertEqual(dict(target.manifest.entries), {})

    def test_replicated_manifest_is_valid_but_conflicting_ids_are_rejected(self):
        source, _ = self.published()
        manifest = next(event for event in self.sink.events if event["type"] == "artifact")
        replica = copy.deepcopy(manifest)
        replica.update(event_id="replica", run_id="fork-child")
        replica["payload"]["history_step"] = 100
        self.sink.events.append(replica)
        uri = source.get_entry("weights.bin").ref_url()
        self.assertEqual(self.target().add_reference(uri)[0]._read_verified_bytes(), b"original immutable bytes")
        for change in ("project", "content"):
            conflict = copy.deepcopy(manifest)
            conflict["event_id"] = "conflict"
            if change == "project":
                conflict["project"] = "different-project"
            else:
                conflict["payload"]["metadata"] = {"model": "different"}
            self.sink.events.append(conflict)
            target = self.target()
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "ambiguous"):
                target.add_reference(uri)
            self.assertEqual(dict(target.manifest.entries), {})
            self.sink.events.pop()

    def test_permissions_and_corrupt_bytes_fail_without_mutation_or_fallback(self):
        source, _ = self.published()
        uri = source.get_entry("weights.bin").ref_url()
        target = self.target()
        with patch.object(self.sink, "read_events", side_effect=PermissionError("permission denied")), patch("backprop.transport.native_store", side_effect=AssertionError("identity fallback")):
            with self.assertRaises(PermissionError):
                target.add_reference(uri)
        with patch.object(self.sink, "download_file", return_value=b"corrupt"):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                target.add_reference(uri)
        self.assertEqual(dict(target.manifest.entries), {})


if __name__ == "__main__":
    unittest.main()
