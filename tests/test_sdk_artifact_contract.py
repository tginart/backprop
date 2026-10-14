"""Immutable artifact contracts through the same Journal used by Foundry.

Architecture: local artifact snapshots -> Run/Journal -> MemorySink manifests ->
fresh remote Artifact -> verified bytes/typed values. Tests intentionally mutate
source files and corrupt remote data to exercise meaningful failure boundaries.
"""
import base64
from datetime import date, datetime, timezone
import gc
import io
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.artifacts import Artifact, ArtifactManifestEntry
from backprop.data_types import Table, Image, Audio, Html, Encoder
from backprop.transport import MemorySink, sync

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5i8AAAAASUVORK5CYII=")


class ArtifactContracts(unittest.TestCase):
    def test_wait_deadline_bounds_inflight_upload_and_publish_without_losing_retry(self):
        for operation in ("upload_file", "publish"):
            for fail in (False, True):
                with self.subTest(operation=operation, fail=fail):
                    artifact = Artifact("deadline-" + operation + str(fail), "model")
                    artifact.add_file(self.create_file(operation + str(fail), b"deadline snapshot"))
                    self.run.log_artifact(artifact)
                    initial_ack = self.run._journal.ack
                    entered, release = threading.Event(), threading.Event()
                    original = getattr(self.sink, operation)
                    def blocked(*args):
                        entered.set()
                        if not release.wait(3):
                            raise RuntimeError("Test network call was not released")
                        if fail:
                            raise OSError("Simulated failure before remote acceptance")
                        return original(*args)
                    with patch.object(self.sink, operation, side_effect=blocked):
                        try:
                            started = time.monotonic()
                            with self.assertRaises(TimeoutError):
                                artifact.wait(timeout=0.05)
                            self.assertLess(time.monotonic() - started, 0.75)
                            self.assertTrue(entered.is_set())
                            self.assertEqual(self.run._journal.ack, initial_ack)
                        finally:
                            release.set()
                            self.assertTrue(self.run._journal._upload_lock.acquire(timeout=2))
                            self.run._journal._upload_lock.release()
                    if fail or operation == "upload_file":
                        self.assertEqual(self.run._journal.ack, initial_ack)
                        self.assertEqual(artifact.state, "PENDING")
                    else:
                        self.assertEqual(artifact.state, "COMMITTED")
                    artifact.wait(timeout=2)
                    self.assertEqual(artifact.state, "COMMITTED")
                    matching = [event for event in self.sink.events if event["type"] == "artifact"
                                and event["payload"]["artifact_id"] == artifact.id]
                    self.assertEqual(len(matching), 1)
                    all_ids = [event["event_id"] for event in self.sink.events]
                    self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_save_creates_finishes_and_publishes_an_automatic_run(self):
        self.run.finish()
        self.assertIsNone(backprop.run)
        artifact = Artifact("auto-save", "model")
        artifact.add_file(self.create_file(data=b"automatic snapshot"))
        sink = MemorySink()
        original_init = backprop.init
        created = []
        def create_auto_run(**kwargs):
            run = original_init(**kwargs, _sink=sink, dir=self.directory / "automatic")
            created.append(run)
            return run
        settings = backprop.Settings(console="off", flush_interval=3600)
        with patch("backprop.init", side_effect=create_auto_run) as initialize:
            self.assertIsNone(artifact.save(project="automatic-artifact-test", settings=settings))
            artifact.save(project="ignored-already-logged")
        self.assertEqual(initialize.call_count, 1)
        self.assertEqual(created[0].project, "automatic-artifact-test")
        self.assertEqual(created[0].job_type, "auto")
        self.assertTrue(created[0]._closed)
        self.assertIsNone(backprop.run)
        self.assertEqual(artifact.state, "COMMITTED")
        manifests = [event["payload"] for event in sink.events if event["type"] == "artifact"]
        self.assertEqual(len(manifests), 1)
        self.assertTrue(any(event["type"] == "run_finish" for event in sink.events))
        self.assertEqual(sink.download_file(manifests[0]["files"][0]), b"automatic snapshot")

    def test_public_files_filter_cursor_and_file_handle_return(self):
        from backprop.apis.public.files import File
        from backprop.apis.public.artifacts import ArtifactFiles
        artifact = Artifact("public-files", "dataset")
        for name in ["a.txt", "b.txt", "c.txt"]:
            artifact.add_file(self.create_file(name, name.encode()))
        remote, _ = self.publish(artifact)
        files = remote.files(names=["a.txt", "c.txt"], per_page=1)
        self.assertIsInstance(files, ArtifactFiles)
        first = next(iter(files))
        self.assertIsInstance(first, File)
        self.assertEqual(first.name, "a.txt")
        with first.download(self.directory / "public-files") as handle:
            self.assertEqual(handle.read(), "a.txt")
        following = remote.files(names=["a.txt", "c.txt"], per_page=1, start=files.cursor)
        self.assertEqual([file.name for file in following], ["c.txt"])
        self.assertEqual(files.path, ["team", "artifact-tests", remote.source_name])
        self.assertIsInstance(remote.get_entry("a.txt").download(self.directory / "entries"), str)

    def test_artifact_read_metadata_lineage_collection_and_checkout(self):
        artifact = Artifact("metadata", "dataset")
        artifact.add_file(self.create_file("a.txt", b"a"))
        remote, payload = self.publish(artifact)
        self.assertEqual(remote.name, "metadata:" + remote.version)
        self.assertEqual(remote.source_name, remote.name)
        self.assertEqual(remote.qualified_name, "team/artifact-tests/" + remote.name)
        self.assertEqual(remote.collection.name, "metadata")
        self.assertEqual(remote.logged_by().id, self.run.id)
        self.run.use_artifact(remote)
        self.run.flush()
        self.assertEqual([run.id for run in remote.used_by()], [self.run.id])
        self.assertEqual(remote.created_at, payload["created_at"])
        self.assertEqual(remote.updated_at, remote.created_at)
        self.assertEqual(remote.history_step, self.run.step)
        destination = self.directory / "checkout"
        destination.mkdir()
        (destination / "obsolete.txt").write_text("obsolete")
        remote.checkout(destination)
        self.assertEqual([path.name for path in destination.iterdir()], ["a.txt"])
        remote.verify(destination)

    def test_metadata_validation_and_date_value_roundtrip(self):
        self.assertEqual(Artifact("metadata", "dataset", metadata='{"value": 1}').metadata, {"value": 1})
        self.assertIsNone(Artifact("metadata", "dataset", metadata={"missing": float("nan")}).metadata["missing"])
        with self.assertRaises(ValueError):
            Artifact("metadata", "dataset", metadata={"infinity": float("inf")})
        with self.assertRaises(ValueError):
            Artifact("metadata", "dataset", metadata={str(index): index for index in range(101)})
        with self.assertRaises(ValueError):
            Artifact("job", "job")
        code = Artifact._new_internal("source-test", "code")
        code.add_file(self.create_file("source.py", b"pass"))
        restored, _ = self.publish(code)
        self.assertEqual(restored.type, "code")
        values = [date(2026, 10, 3), datetime(2026, 10, 3, 3, 4, tzinfo=timezone.utc)]
        artifact = Artifact("dates", "dataset")
        artifact.add(Table(columns=["time"], data=[[value] for value in values]), "table")
        remote, _ = self.publish(artifact)
        self.assertEqual([row[0] for row in remote.get("table").data], values)

    @unittest.skipUnless(importlib.util.find_spec("numpy") is not None, "NumPy is optional")
    def test_ndarray_table_cell_roundtrip_preserves_shape_dtype_and_nonfinite(self):
        import numpy as np
        values = np.array([[1, float("inf")], [3, 4]], dtype=np.float32)
        table = Table(columns=["matrix"], data=[[values]])
        artifact = Artifact("arrays", "dataset")
        artifact.add(table, "table")
        remote, _ = self.publish(artifact)
        restored = remote.get("table").data[0][0]
        self.assertEqual(restored.dtype, values.dtype)
        np.testing.assert_array_equal(restored, values)

    @unittest.skipUnless(importlib.util.find_spec("numpy") is not None, "NumPy is optional")
    def test_ndarray_empty_axes_and_object_cells_roundtrip(self):
        import numpy as np
        from backprop.object_codec import decode_ndarray
        objects = np.empty(2, dtype=object)
        objects[0], objects[1] = [1, 2], {"nested": [3, 4]}
        values = [np.empty((0, 3)), np.empty((2, 0, 3)), objects]
        artifact = Artifact("empty-arrays", "dataset")
        for index, value in enumerate(values):
            artifact.add(Table(columns=["value"], data=[[value]]), str(index))
        remote, _ = self.publish(artifact)
        for index, value in enumerate(values):
            actual = remote.get(str(index)).data[0][0]
            self.assertEqual(actual.shape, value.shape)
            self.assertEqual(actual.dtype, value.dtype)
            self.assertEqual(actual.tolist(), value.tolist())
        for shape, data, dtype in [([2, 0, 3], [], "float64"), ([0, 3], [[], []], "float64"),
                                   ([True], [1], "float64"), ([-1], [], "object"),
                                   ([2], [[1]], "object")]:
            with self.assertRaises(ValueError):
                decode_ndarray({"shape": shape, "dtype": dtype}, data)

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.directory = Path(self.scratch.name)
        self.sink = MemorySink()
        self.run = backprop.init(project="artifact-tests", entity="team", dir=self.directory / "run",
                                 _sink=self.sink, settings=backprop.Settings(flush_interval=3600, console="off"))
        self.addCleanup(self.run.finish)

    def create_file(self, name="weights.bin", data=b"generation one"):
        file = self.directory / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)
        return file

    def publish(self, artifact, **kwargs):
        self.assertIs(self.run.log_artifact(artifact, **kwargs).wait(), artifact)
        payload = [event["payload"] for event in self.sink.events if event["type"] == "artifact"][-1]
        return Artifact._from_manifest(payload, self.sink, "team", "artifact-tests", self.run.id), payload

    def test_add_file_returns_entry_and_snapshots_bytes_immediately(self):
        source = self.create_file()
        artifact = Artifact("weights", "model")
        entry = artifact.add_file(source, "checkpoints/model.bin")
        self.assertIsInstance(entry, ArtifactManifestEntry)
        self.assertIs(entry.parent_artifact(), artifact)
        self.assertEqual(artifact.get_added_local_path_name(source), entry.path)
        source.write_bytes(b"changed after add")
        remote, _ = self.publish(artifact)
        self.assertEqual(Path(remote.file(self.directory / "download")).read_bytes(), b"generation one")
        self.assertEqual(remote.files()[0].md5, entry.digest)
        self.assertEqual(remote.get_entry(entry.path).sha256, entry.sha256)
        remote.verify(self.directory / "download")

    def test_metadata_description_and_tags_are_part_of_immutable_version(self):
        source = self.create_file()
        artifacts = []
        for metadata, description, tags in [({"epoch": 1}, "first", []), ({"epoch": 2}, "first", []),
                                            ({"epoch": 1}, "other", []), ({"epoch": 1}, "first", ["candidate"])]:
            artifact = Artifact("same-collection", "model", metadata=metadata, description=description)
            artifact.add_file(source)
            remote, payload = self.publish(artifact, tags=tags)
            self.assertEqual(remote.digest, payload["digest"])
            self.assertEqual(remote.version, "sha256-" + remote.digest)
            artifacts.append(remote)
        self.assertEqual(len({artifact.version for artifact in artifacts}), 4)
        repeat = Artifact("same-collection", "model", metadata={"epoch": 1}, description="first")
        repeat.add_file(source)
        remote, _ = self.publish(repeat)
        self.assertEqual(remote.version, artifacts[0].version)

    def test_finalize_freezes_files_but_allows_descriptive_edits(self):
        artifact = Artifact("frozen", "dataset", metadata={"nested": [{"value": 1}]})
        artifact.add_file(self.create_file())
        artifact.finalize()
        artifact.metadata["nested"][0]["value"] = 2
        artifact.metadata["nested"].append(3)
        artifact.description = "mutated"
        self.assertEqual(artifact.metadata, {"nested": [{"value": 2}, 3]})
        self.assertEqual(artifact.description, "mutated")
        with self.assertRaises(TypeError):
            artifact.manifest.entries["new"] = next(iter(artifact.manifest.entries.values()))
        with self.assertRaises(RuntimeError):
            artifact.add_file(self.create_file("second.bin"))

    def test_remote_listing_allocates_no_temporary_directory(self):
        payload = {"name": "remote", "artifact_type": "dataset", "files": [], "metadata": {}}
        with patch("backprop.artifacts.tempfile.TemporaryDirectory", side_effect=AssertionError("unexpected scratch allocation")):
            remote = Artifact._from_manifest(payload, self.sink)
            self.assertEqual(list(remote.files()), [])
            self.assertIsNone(remote._temporary)

    def test_directory_merge_remove_and_failed_new_file(self):
        source = self.create_file("directory/a.txt", b"a").parent
        self.create_file("directory/sub/b.txt", b"b")
        artifact = Artifact("directory", "dataset")
        artifact.add_dir(source, name="prefix")
        self.assertEqual(set(artifact.manifest.entries), {"prefix/a.txt", "prefix/sub/b.txt"})
        artifact.add_dir(source, name="prefix")
        self.assertEqual(len(artifact.manifest.entries), 2)
        (source / "a.txt").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            artifact.add_dir(source, name="prefix")
        self.assertEqual(len(artifact.manifest.entries), 2)
        artifact.remove("prefix/sub")
        self.assertEqual(len(artifact.manifest.entries), 1)
        with self.assertRaises(RuntimeError):
            with artifact.new_file("failed.txt") as stream:
                stream.write("partial")
                raise RuntimeError("writer failed")
        self.assertNotIn("failed.txt", artifact.manifest.entries)
        with artifact.new_file("complete.txt") as stream:
            stream.write("complete")
        self.assertIn("complete.txt", artifact.manifest.entries)

    def test_mutable_aliases_and_path_traversal_validation(self):
        artifact = Artifact("safe", "dataset")
        source = self.create_file()
        for name in ["../escape", "/absolute", "C:/drive", "x/../../escape", "bad\0name"]:
            with self.assertRaises(ValueError):
                artifact.add_file(source, name)
        artifact.add_file(source)
        self.run.log_artifact(artifact, aliases=["latest"])
        artifact.wait()
        self.assertEqual(artifact.aliases, ["latest"])
        with self.assertRaises(ValueError): artifact.aliases = ["bad:alias"]

    def test_corrupt_download_does_not_replace_existing_destination(self):
        artifact = Artifact("checksum", "dataset")
        artifact.add_file(self.create_file())
        remote, payload = self.publish(artifact)
        destination = self.directory / "download"
        destination.mkdir()
        local = destination / "weights.bin"
        local.write_bytes(b"existing valuable bytes")
        with patch.object(self.sink, "download_file", return_value=b"wrong"):
            with self.assertRaises(ValueError):
                remote.get_entry("weights.bin").download(destination, skip_cache=True)
        self.assertEqual(local.read_bytes(), b"existing valuable bytes")

    def test_cached_download_is_verified_before_skip_cache_policy(self):
        artifact = Artifact("cache", "dataset")
        artifact.add_file(self.create_file())
        remote, _ = self.publish(artifact)
        destination = self.directory / "cache"
        remote.download(destination)
        with patch.object(self.sink, "download_file", side_effect=AssertionError("unexpected network")):
            remote.download(destination)
            # Pinned entry.download checks an already-correct destination
            # before deciding whether to bypass the backend cache.
            remote.download(destination, skip_cache=True)

    def test_table_and_nested_media_roundtrip_preserve_full_snapshot(self):
        table = Table(columns=["score", "image", "details"], data=[[1.0, Image(PNG, caption="sample"), {"flags": [True]}]], log_mode="INCREMENTAL")
        encoder = Encoder(lambda *args, **kwargs: {})
        encoder.encode(table); encoder.commit()
        artifact = Artifact("rich", "dataset")
        entry = artifact.add(table, "predictions")
        self.assertEqual(entry.path, "predictions.table.json")
        self.assertIs(artifact.add(table, "duplicate-object"), entry)
        table.add_data(2.0, Image(PNG), {"flags": [False]})
        self.assertEqual(table._offset, 1)
        remote, _ = self.publish(artifact)
        restored = remote.get("predictions")
        self.assertEqual(len(restored.data), 1)
        self.assertIsInstance(restored.data[0][1], Image)
        self.assertEqual(restored.data[0][1]._bytes(), PNG)
        self.assertEqual(restored.data[0][1].caption, "sample")
        self.assertEqual(restored._types[0].kind, "number")
        self.assertEqual(restored._types[2].fields["flags"].item_type.kind, "boolean")
        self.assertIsNone(remote.get("absent"))

    def test_audio_html_objects_and_reference_encoding(self):
        artifact = Artifact("values", "dataset")
        artifact["audio"] = Audio([0, 0.5], sample_rate=8000)
        artifact["html"] = Html("<b>Hello</b>", inject=False)
        with self.assertRaises(RuntimeError):
            Encoder(lambda *args, **kwargs: {}).encode(artifact)
        remote, _ = self.publish(artifact)
        self.assertEqual(remote["audio"].metadata["sample_rate"], 8000)
        self.assertEqual(remote["html"].html, "<b>Hello</b>")
        ref = Encoder(lambda *args, **kwargs: {}).encode(remote)
        self.assertEqual(ref["version"], remote.version)
        self.assertEqual(ref["artifact_id"], remote.id)

    def test_table_relationships_roundtrip_reconstruct_source_objects(self):
        source = Table(columns=["id", "value"], data=[["a", 1], ["b", 2]])
        source.set_pk("id")
        related = Table(columns=["key", "row"], data=[[source.get_column("id")[1], source.index_ref(1)]])
        artifact = Artifact("linked", "dataset")
        artifact.add(related, "related")
        remote, _ = self.publish(artifact)
        restored = remote.get("related")
        self.assertEqual(restored.data[0][1].get_row(), {"id": "b", "value": 2})
        self.assertIs(restored.data[0][0]._table, restored.data[0][1]._table)

    def test_commit_acknowledgement_timeout_and_immutable_new_draft(self):
        artifact = Artifact("lifecycle", "dataset", metadata={"nested": [1]})
        self.assertIsNone(artifact.id)
        entry = artifact.add_file(self.create_file())
        with self.assertRaises(ValueError):
            entry.ref_url()
        self.run.log_artifact(artifact)
        self.assertEqual(artifact.state, "PENDING")
        self.assertIsNone(artifact.id)
        self.assertTrue(artifact.is_draft())
        with self.assertRaises(TimeoutError):
            artifact.wait(timeout=0)
        artifact.wait(timeout=5)
        self.assertEqual(artifact.state, "COMMITTED")
        self.assertIsInstance(artifact.id, str)
        self.assertEqual(entry.ref_url(), f"backprop-artifact://{artifact.id}/weights.bin")
        self.assertFalse(artifact.is_draft())
        draft = artifact.new_draft()
        self.assertEqual(draft.state, "PENDING")
        self.assertIsNone(draft.id)
        draft.metadata["nested"].append(2)
        draft.add_file(self.create_file("new.txt", b"new"))
        remote, _ = self.publish(draft)
        self.assertNotEqual(remote.version, artifact.version)
        self.assertEqual(artifact.metadata["nested"], [1])
        self.assertEqual(artifact.file_count, 1)
        self.assertEqual(remote.file_count, 2)

    def test_offline_artifact_references_keep_identity_until_external_sync_commits(self):
        self.run.finish()
        run = backprop.init(project="artifact-tests", entity="team", dir=self.directory / "offline",
                            mode="offline", settings={"console": "off", "silent": True})
        self.addCleanup(run.finish)
        artifact = Artifact("queued", "dataset")
        artifact.add_file(self.create_file())
        run.log_artifact(artifact)
        run.config["dataset"] = artifact
        with self.assertRaisesRegex(TypeError, "Cannot use artifact when in offline mode"):
            run.use_artifact(artifact)
        run.finish()
        self.assertIsNone(artifact.id)
        from backprop.artifact_identity import ArtifactNotLoggedError
        with self.assertRaisesRegex(ArtifactNotLoggedError, "while in offline mode"):
            artifact.wait()
        original_ack = run._journal.ack
        sync(run.sync_dir, sink=self.sink)
        self.assertEqual(run._journal.ack, original_ack)
        self.assertIs(artifact.wait(timeout=0), artifact)
        self.assertEqual(artifact.state, "COMMITTED")
        self.assertIsInstance(artifact.id, str)
        manifests = [event["payload"] for event in self.sink.events if event["type"] == "artifact"]
        uses = [event["payload"] for event in self.sink.events if event["type"] == "artifact_use"]
        self.assertEqual(manifests[-1]["artifact_id"], artifact.id)
        self.assertEqual([item["artifact_id"] for item in uses], [artifact.id])
        remote = backprop.Api._from_store(self.sink).run("team/artifact-tests/" + run.id)
        self.assertEqual(remote.config["dataset"]["artifact_id"], artifact.id)

    def test_default_checkout_rejects_unsafe_names_before_touching_local_files(self):
        original = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, original)
        sentinel = self.create_file("keep.txt", b"preserve me")
        for name in ["..", ".", "", "..\\outside"]:
            # Hydrated identities need path safety even if creation rejects them.
            artifact = Artifact._new_internal(name, "dataset")
            artifact.add_file(self.create_file("safe.bin", b"snapshot"))
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    artifact.checkout()
                with self.assertRaises(ValueError):
                    artifact.download()
                self.assertEqual(sentinel.read_bytes(), b"preserve me")
        artifact = Artifact("linked", "dataset")
        artifact.add_file(sentinel)
        Path("artifacts").mkdir()
        Path("artifacts/linked").symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            artifact.checkout()
        self.assertEqual(sentinel.read_bytes(), b"preserve me")
        self.assertFalse((self.directory.parent / "safe.bin").exists())

    def test_stored_unicode_and_colon_names_roundtrip_without_weakening_path_validation(self):
        original = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, original)
        for name in ["训练 data", "models:best checkpoint", "a", "a:b"]:
            with self.subTest(name=name):
                # Existing collection identities have a broader naming boundary
                # than public creation. Exercise restoration without weakening it.
                artifact = Artifact._new_internal(name, "dataset")
                artifact.add_file(self.create_file())
                remote, _ = self.publish(artifact)
                queried = backprop.Api._from_store(self.sink).artifact("team/artifact-tests/" + remote.name)
                self.assertEqual(queried.id, remote.id)
                self.assertTrue(Path(queried.download()).is_relative_to((self.directory / "artifacts").resolve()))
                self.run.use_artifact(queried)
                self.run.flush()
                self.assertEqual([run.id for run in queried.used_by()], [self.run.id])
        for name in ["a/b", "x" * 129]:
            with self.assertRaises(ValueError):
                Artifact(name, "dataset")

    def test_entry_optional_size_and_reference_errors(self):
        source = self.create_file()
        entry = ArtifactManifestEntry(path=Path("weights.bin"), digest="", local_path=source)
        self.assertEqual(entry.path, "weights.bin")
        self.assertEqual(entry.local_path, str(source))
        self.assertEqual(entry.size, source.stat().st_size)
        self.assertIsNone(ArtifactManifestEntry(path="unknown", digest="").size)
        with self.assertRaises(ValueError):
            ArtifactManifestEntry(path="invalid", digest="", size=-1)
        with self.assertRaises(NotImplementedError):
            entry.parent_artifact()
        with self.assertRaises(ValueError):
            entry.ref_target()
        self.assertEqual(ArtifactManifestEntry(path="ref", digest="", ref="file:///source").ref_target(), "file:///source")

    def test_native_entry_is_durable_but_file_reference_checks_original_source(self):
        source = self.create_file()
        original = Artifact("original", "dataset")
        original.add_file(source)
        remote, _ = self.publish(original)
        copied = Artifact("references", "dataset")
        self.assertEqual(copied.add_reference(remote.get_entry("weights.bin"), "copy.bin")[0].path, "copy.bin")
        copied.add_reference(source.as_uri(), "local.bin")
        source.write_bytes(b"later mutation")
        result, _ = self.publish(copied)
        self.assertEqual(Path(result.get_entry("copy.bin").download(self.directory / "references")).read_bytes(), b"generation one")
        with self.assertRaisesRegex(ValueError, "Digest mismatch"):
            result.get_entry("local.bin").download(self.directory / "references")

    def test_failed_typed_object_add_rolls_back_dependency_manifest(self):
        source = Table(columns=["id"], data=[["a"]])
        source.set_pk("id")
        table = Table(columns=["key", "unsupported"], data=[[source.get_column("id")[0], object()]])
        artifact = Artifact("atomic", "dataset")
        with self.assertRaises(TypeError):
            artifact.add(table, "table")
        self.assertEqual(dict(artifact.manifest.entries), {})
        self.assertEqual(artifact._objects, {})

    def test_http_reference_keeps_provider_metadata_and_downloads_on_read(self):
        from test_sdk_external_references import Response
        artifact = Artifact("http", "dataset")
        uri = "https://example.test/data.bin"
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=lambda *a, **k: Response(uri, b"downloaded later")) as request:
            artifact.add_reference(uri)
            self.assertEqual(request.call_count, 1)
            remote, _ = self.publish(artifact)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(Path(remote.file(self.directory / "http")).read_bytes(), b"downloaded later")
            self.assertEqual(request.call_count, 2)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=AssertionError("no read")):
            unchecksummed = Artifact("uri-only", "dataset").add_reference(uri, checksum=False)[0]
        self.assertEqual(unchecksummed.digest, uri)


if __name__ == "__main__":
    unittest.main()
