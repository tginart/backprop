"""Artifact entry origins survive native immutable snapshot lifecycles.

Architecture: draft -> durable manifest -> upload -> public read -> reference
or new draft. Verify the origin fields at each boundary without changing content
versions. Fault injection proves failed writes do not announce an origin; mocked
HTTP and local files prove external references retain their original provider.
Main callers: unittest discovery. No service credentials or optional packages.
"""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.artifact_events import iter_artifact_events
from backprop.apis.public.replay import reduce_events
from backprop.transport import MemorySink, read_journal


class ArtifactEntryProvenance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.bin"
        self.source.write_bytes(b"immutable source bytes")
        self.store = MemorySink()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.run = backprop.Run(entity="owner", project="provenance", id="writer", dir=self.root,
            settings={"silent": True, "console": "off", "disable_git": True, "flush_interval": 3600},
            _sink=self.store, _deployment={})
        self.api = backprop.Api._from_store(self.store, entity="owner", project="provenance")

    def tearDown(self):
        if not self.run._closed:
            self.run.finish()
        self.environment.stop()
        self.temporary.cleanup()

    def publish(self, name="original", path="file.bin"):
        artifact = backprop.Artifact(name, "dataset")
        entry = artifact.add_file(self.source, path)
        self.run.log_artifact(artifact)
        self.run.flush()
        return artifact, entry

    def test_commit_birth_roundtrip_does_not_change_content_digest(self):
        artifact = backprop.Artifact("original", "dataset")
        entry = artifact.add_file(self.source)
        digest = artifact.digest
        manifest_digest = artifact.manifest.digest()
        self.assertIsNone(entry.birth_artifact_id)
        self.run.log_artifact(artifact)
        self.assertEqual(entry.birth_artifact_id, artifact._id)
        self.assertEqual((artifact.digest, artifact.manifest.digest()), (digest, manifest_digest))
        self.assertIsNone(entry.ref)
        event = next(iter_artifact_events(read_journal(self.run.sync_dir)))
        self.assertEqual(event["payload"]["files"][0]["birth_artifact_id"], artifact._id)
        self.run.flush()
        remote = self.api.artifact(artifact.id).get_entry(entry.path)
        self.assertEqual(remote.birth_artifact_id, artifact.id)
        self.assertEqual(remote.to_json()["birthArtifactID"], artifact.id)
        self.assertEqual(remote._read_verified_bytes(), self.source.read_bytes())

    def test_failed_append_leaves_birth_unassigned_then_retry_keeps_native_id(self):
        artifact = backprop.Artifact("retry", "dataset")
        entry = artifact.add_file(self.source)
        identity = artifact._id
        with patch.object(self.run, "_emit", side_effect=OSError("journal write failed")):
            with self.assertRaisesRegex(OSError, "journal write failed"):
                self.run.log_artifact(artifact)
        self.assertIsNone(entry.birth_artifact_id)
        self.assertFalse(artifact._logged)
        self.run.log_artifact(artifact)
        self.assertEqual((artifact._id, entry.birth_artifact_id), (identity, identity))

    def test_native_entry_url_and_chain_preserve_origin_and_escaped_target(self):
        source, original = self.publish(path="folder/source ?#雪.bin")
        original_json = original.to_json()
        source_url = original.ref_url()
        first = backprop.Artifact("first", "dataset")
        entry = first.add_reference(original, name="renamed.bin")[0]
        self.assertEqual((entry.ref, entry.birth_artifact_id), (source_url, source.id))
        self.run.log_artifact(first)
        self.run.flush()
        remote = self.api.artifact(first.id).get_entry("renamed.bin")
        second = backprop.Artifact("second", "dataset")
        second._store = self.store
        copied = second.add_reference(remote.ref_url(), name="again.bin")[0]
        self.assertEqual((copied.ref, copied.ref_target(), copied.birth_artifact_id),
                         (source_url, source_url, source.id))
        self.assertEqual(original.to_json(), original_json)
        self.run.log_artifact(second)
        self.run.flush()
        final = self.api.artifact(second.id).get_entry("again.bin")
        self.assertEqual((final.ref, final.birth_artifact_id), (source_url, source.id))
        self.assertEqual(final._read_verified_bytes(), b"immutable source bytes")
        self.source.unlink()
        self.store.files.clear()
        self.assertEqual(copied._read_verified_bytes(), b"immutable source bytes")

    def test_new_draft_preserves_origin_until_replacement_and_equal_bytes_do_not_infer_birth(self):
        source, original = self.publish()
        draft = self.api.artifact(source.id).new_draft()
        inherited = draft.get_entry(original.path)
        self.assertEqual(inherited.birth_artifact_id, source.id)
        self.assertIsNone(inherited.ref)
        self.assertEqual(draft.digest, source.digest)
        changed = self.root / "changed.bin"
        changed.write_bytes(b"new bytes")
        replacement = draft.add_file(changed, original.path, overwrite=True)
        self.assertIsNone(replacement.birth_artifact_id)
        self.run.log_artifact(draft)
        self.run.flush()
        self.assertEqual(replacement.birth_artifact_id, draft.id)
        self.assertEqual(original.birth_artifact_id, source.id)
        independent, independent_entry = self.publish()
        self.assertEqual(independent.version, source.version)
        self.assertNotEqual(independent.id, source.id)
        self.assertEqual(independent_entry.birth_artifact_id, independent.id)

    def test_external_uri_provenance_keeps_provider_reads_and_draft_metadata(self):
        from test_sdk_external_references import Response
        artifact = backprop.Artifact("external", "dataset")
        file_uri = self.source.as_uri()
        local = artifact.add_reference(file_uri, name="local.bin")[0]
        uri = "https://example.invalid/data/source.bin?version=3"
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", return_value=Response(uri, b"HTTP provider bytes")) as request:
            remote = artifact.add_reference(uri, name="remote.bin")[0]
        self.assertEqual(request.call_count, 1)
        self.assertEqual((local.ref, local.ref_target()), (file_uri, file_uri))
        self.assertEqual((remote.ref, remote.ref_target()), (uri, uri))
        self.source.unlink()
        self.run.log_artifact(artifact)
        self.run.flush()
        loaded = self.api.artifact(artifact.id)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", return_value=Response(uri, b"HTTP provider bytes")):
            self.assertEqual(loaded.get_entry("remote.bin")._read_verified_bytes(), b"HTTP provider bytes")
        with self.assertRaisesRegex(ValueError, "Failed to find"):
            loaded.get_entry("local.bin")._read_verified_bytes()
        for name, reference in (("local.bin", file_uri), ("remote.bin", uri)):
            entry = loaded.get_entry(name)
            self.assertEqual((entry.ref, entry.birth_artifact_id), (reference, artifact.id))
        draft = loaded.new_draft()
        self.assertEqual(draft.get_entry("remote.bin").ref, uri)
        self.assertEqual(draft.get_entry("remote.bin").birth_artifact_id, artifact.id)

    def test_file_directory_reference_retains_pinned_unescaped_child_path_quirk(self):
        directory = self.root / "data ?#雪"
        (directory / "nested").mkdir(parents=True)
        source = directory / "nested/name #?.bin"
        source.write_bytes(b"nested")
        artifact = backprop.Artifact("directory", "dataset")
        entries = artifact.add_reference(directory.as_uri(), name="prefix")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "prefix/nested/name #?.bin")
        self.assertEqual(entries[0].ref, directory.as_uri() + "/nested/name #?.bin")
        # Pinned local handler joins raw child names, so a # in a child name
        # is interpreted as a URI fragment when the provider later reads it.
        with self.assertRaisesRegex(ValueError, "Failed to find"):
            entries[0]._read_verified_bytes()

    def test_conflicting_explicit_origins_are_rejected_without_changing_original(self):
        artifact, entry = self.publish()
        original = next(iter_artifact_events(self.store.events))
        before = copy.deepcopy(original)
        for field in ("birth_artifact_id", "ref"):
            conflict = copy.deepcopy(original)
            conflict["event_id"] = "conflicting-origin"
            conflict["payload"]["files"][0][field] = "other-origin"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "ambiguous"):
                list(iter_artifact_events([original, conflict]))
        self.assertEqual(original, before)
        self.assertEqual(entry.birth_artifact_id, artifact.id)

    def test_historical_missing_origins_enrich_first_read_without_hiding_explicit_conflicts(self):
        artifact, entry = self.publish()
        current = next(iter_artifact_events(self.store.events))
        historical = copy.deepcopy(current)
        historical["event_id"] = "historical"
        for reference in historical["payload"]["files"]:
            reference.pop("birth_artifact_id")
            reference.pop("ref")
        original = copy.deepcopy(historical)
        output = list(iter_artifact_events([historical, current]))
        for event in output:
            self.assertEqual(event["payload"]["files"][0]["birth_artifact_id"], artifact.id)
            self.assertIsNone(event["payload"]["files"][0]["ref"])
        replay = reduce_events([historical, current])
        self.assertEqual(len(replay["artifacts"]), 1)
        self.assertEqual(replay["artifacts"][0]["files"][0]["birth_artifact_id"], artifact.id)
        self.assertEqual(historical, original)
        conflict = copy.deepcopy(current)
        conflict["event_id"] = "conflict-after-unknown"
        conflict["payload"]["files"][0]["birth_artifact_id"] = "another-artifact"
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            list(iter_artifact_events([historical, current, conflict]))


if __name__ == "__main__":
    unittest.main()
