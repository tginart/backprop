"""URI Image/Audio construction is lazy; artifact reads retain provider integrity.

Architecture: compare pinned pure URI methods -> reject unsupported direct run
binding atomically -> log ordinary and implicit artifacts -> retrieve bytes with
the same provider metadata. A requests-shaped local fixture replaces only HTTP
I/O; the actual native handlers and artifact manifests remain in the test path.
"""
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib import parse
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.transport import MemorySink, read_journal, sync

SOURCE = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/sdk/data_types"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + b"\0\0\0\1" * 2 + b"sample"


def reference_method(path, cls_name, name):
    """Execute the selected unchanged upstream method without service imports."""
    source = ast.parse((SOURCE / path).read_text())
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == cls_name)
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    method.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[]))
    namespace = {"hashlib": hashlib, "parse": parse, "re": re, "pathlib": __import__("pathlib")}
    exec(compile(module, str(SOURCE / path), "exec"), namespace)
    return namespace[name]


class Response:
    def __init__(self, url, content, reads, etag='"v1"'):
        self.request = SimpleNamespace(url=url)
        self.headers = {"etag": etag, "content-length": str(len(content))}
        self.content, self.reads = content, reads
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, chunk_size):
        self.reads.append(self.request.url)
        yield self.content


class UriMediaContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runs, self.requests, self.byte_reads = [], [], []
        self.store = MemorySink()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        self.environment.stop()
        self.temp.cleanup()

    def new(self, offline=False):
        run = backprop.Run(project="uri-media", entity="tests", id="writer", dir=self.root,
            mode="offline" if offline else "online", _sink=None if offline else self.store,
            _deployment={}, settings={"console": "off", "silent": True, "disable_git": True,
                                      "flush_interval": 3600})
        self.runs.append(run)
        return run

    def response(self, url, **kwargs):
        self.requests.append(url)
        return Response(url, PNG, self.byte_reads)

    @unittest.skipUnless(SOURCE.is_dir(), "Pinned source snapshot required")
    def test_constructor_identity_detection_and_image_copy_match_pinned_methods(self):
        detect = reference_method("base_types/media.py", "Media", "path_is_reference")
        image_init = reference_method("image.py", "Image", "_initialize_from_reference")
        for value in (None, "", "https://x/image.png?token=value", "http://x", "gs://b/key", "s3://b/key", "file:///tmp/image", "ftp://x", "HTTPS://x", "Gs://b/key", " https://x", "https://x ", Path("http:/x")):
            self.assertEqual(backprop.Image.path_is_reference(value), detect(value))
        uri = "https://example.invalid/image.png?signature=private"
        expected = SimpleNamespace()
        image_init(expected, uri)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=AssertionError("constructor I/O")):
            image, audio = backprop.Image(uri), backprop.Audio(uri, caption="clip")
            copied = backprop.Image(image)
        self.assertEqual((image._path, image._sha256, image.format), (expected._path, expected._sha256, expected.format))
        self.assertEqual(audio._sha256, expected._sha256)
        self.assertEqual(audio.resolve_ref(), uri)
        self.assertIsNone(image.image)
        self.assertEqual(image.to_data_array(), [])
        self.assertTrue(image.file_is_set())
        self.assertEqual(image, copied)
        self.assertEqual(image, backprop.Image(uri, caption="ignored in URI equality"))
        self.assertNotEqual(audio, backprop.Audio(uri, caption="different"))

    @unittest.skipUnless(SOURCE.is_dir(), "Pinned source snapshot required")
    def test_direct_binding_matches_source_error_and_row_is_atomic(self):
        run = self.new()
        for cls, filename in [(backprop.Image, "image.py"), (backprop.Audio, "audio.py")]:
            value = cls("https://example.invalid/file")
            pinned = reference_method(filename, cls.__name__, "bind_to_run")
            with self.assertRaises(ValueError) as expected:
                pinned(value, run, "value", 0)
            with self.assertRaises(ValueError) as actual:
                run.log({"valid": 1, "value": value})
            self.assertEqual(str(actual.exception), str(expected.exception))
            with self.assertRaises(ValueError):
                run.log({"batch": [value, value]})
        self.assertFalse(any(event["type"] == "history" for event in read_journal(run.sync_dir)))

    def test_explicit_artifact_metadata_then_verified_retrieval_without_uploading_provider_bytes(self):
        uri = "https://example.invalid/image.png"
        image = backprop.Image(uri, caption="reference")
        artifact = backprop.Artifact("references", "dataset")
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response):
            object_entry = artifact.add(image, "image")
        payload = json.loads(object_entry._read_verified_bytes())
        media_entry = artifact.get_entry(payload["path"])
        self.assertEqual(media_entry.digest, "v1")
        self.assertEqual(payload["sha256"], hashlib.sha256(uri.encode()).hexdigest())
        self.assertIsNone(media_entry.sha256)
        self.assertEqual(self.byte_reads, [])
        run = self.new()
        run.log_artifact(artifact)
        run.finish()
        self.assertFalse(any(data == PNG for data in self.store.files.values()))
        api = backprop.Api._from_store(self.store, entity=run.entity, project=run.project)
        remote = api.artifact(artifact.qualified_name)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response):
            restored = remote.get("image")
        self.assertEqual(restored._bytes(), PNG)
        self.assertEqual(restored.caption, "reference")
        self.assertEqual(self.byte_reads, [uri])

    def test_implicit_table_offline_sync_keeps_external_descriptor_and_retrievable_entry(self):
        run = self.new(offline=True)
        image = backprop.Image("https://example.invalid/image.png")
        table = backprop.Table(columns=["image"], data=[[image]])
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response):
            run.log({"examples": table})
        row = next(event for event in read_journal(run.sync_dir) if event["type"] == "history")
        descriptor = row["payload"]["metrics"]["examples"]["data"][0][0]
        self.assertTrue(descriptor["_backprop_external_reference"])
        self.assertEqual(descriptor["uri"], image._reference_uri)
        self.assertNotIn("sha256", descriptor)
        self.assertNotIn("dataset_rid", descriptor)
        self.assertEqual(self.byte_reads, [])
        run.finish()
        sync(run.sync_dir, sink=self.store)
        api = backprop.Api._from_store(self.store, entity=run.entity, project=run.project)
        artifact = list(api.run(run.id).logged_artifacts())[0]
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response):
            recovered = artifact.get("examples")
        self.assertEqual(recovered.data[0][0]._bytes(), PNG)

    def test_audio_reference_download_reconstructs_bytes_and_source_identity(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000); stream.writeframes(b"\0\0" * 4)
        data = buffer.getvalue()
        audio = backprop.Audio("https://example.invalid/clip.wav", caption="clip")
        artifact = backprop.Artifact("audio", "dataset")
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=lambda url, **kwargs: Response(url, data, self.byte_reads)):
            artifact.add(audio, "clip")
            restored = artifact.get("clip")
        self.assertEqual(restored._bytes(), data)
        # Pinned URI audio has no declared sample rate; reconstruction retains
        # its recorded metadata rather than inventing a logged sample_rate.
        self.assertIsNone(restored.metadata["sample_rate"])
        self.assertEqual(restored, audio)

    def test_artifact_failure_rolls_back_reference_entries_and_object_provenance(self):
        artifact = backprop.Artifact("invalid", "dataset")
        image = backprop.Image("https://example.invalid/image.png")
        table = backprop.Table(columns=["image", "bad"], data=[[image, {"v": 1}]])
        table.data[0][1] = object()
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response):
            with self.assertRaises(TypeError):
                artifact.add(table, "bad")
        self.assertEqual(artifact._entries, {})
        self.assertIsNone(image._artifact_target)

    def test_existing_native_or_different_external_entry_cannot_impersonate_uri_media(self):
        uri = "https://example.invalid/image.png"
        image = backprop.Image(uri)
        logical = "media/images/" + image._sha256[:20] + "/image.png"
        artifact = backprop.Artifact("collision", "dataset")
        wrong = self.root / "wrong.png"
        wrong.write_bytes(b"wrong")
        original = artifact.add_file(wrong, name=logical)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response):
            with self.assertRaisesRegex(ValueError, "different reference"):
                artifact.add(image, "image")
        self.assertIs(artifact.get_entry(logical), original)
        self.assertEqual(artifact._objects, {})
        self.assertIsNone(image._artifact_target)
        artifact.remove(logical)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response):
            different = artifact.add_reference("https://example.invalid/other.png", name=logical)[0]
            with self.assertRaisesRegex(ValueError, "different reference"):
                artifact.add(image, "image")
        self.assertIs(artifact.get_entry(logical), different)

    def test_distinct_instances_refresh_metadata_but_same_object_reuses_exact_entry(self):
        from backprop.media_references import descriptor
        uri = "https://example.invalid/image.png"
        artifact = backprop.Artifact("metadata", "dataset")
        first, second = backprop.Image(uri), backprop.Image(uri)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=[
                Response(uri, PNG, [], etag='"first"'), Response(uri, PNG, [], etag='"changed"')]) as get:
            saved = descriptor(first, artifact)
            self.assertEqual(descriptor(first, artifact), saved)
            self.assertEqual(get.call_count, 1)
            with self.assertRaisesRegex(ValueError, "different reference"):
                descriptor(second, artifact)
            self.assertEqual(get.call_count, 2)
        self.assertEqual(artifact.get_entry(saved["path"]).digest, "first")

    def test_removed_replaced_and_rolled_back_entries_invalidate_binding_hints(self):
        from backprop.media_references import descriptor
        uri = "https://example.invalid/image.png"
        artifact = backprop.Artifact("hints", "dataset")
        image = backprop.Image(uri)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response) as get:
            first = descriptor(image, artifact)
            original = artifact.get_entry(first["path"])
            artifact.remove(first["path"])
            descriptor(image, artifact)
            self.assertEqual(get.call_count, 2)
            self.assertIsNot(artifact.get_entry(first["path"]), original)
            # Even an equivalent manual replacement is a new owner entry.
            artifact.add_reference(uri, name=first["path"])
            descriptor(image, artifact)
            self.assertEqual(get.call_count, 4)
        fresh = backprop.Artifact("rollback", "dataset")
        table = backprop.Table(columns=["image", "bad"], data=[[image, 1]])
        table.data[0][1] = object()
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response) as get:
            with self.assertRaises(TypeError):
                fresh.add(table, "failed")
            self.assertEqual(fresh._entries, {})
            descriptor(image, fresh)
            self.assertEqual(get.call_count, 2)
        self.assertIsNone(image._artifact_target)

    def test_provider_conflict_in_implicit_table_preserves_pending_row_and_retries_once(self):
        uri = "https://example.invalid/image.png"
        first, second = backprop.Image(uri), backprop.Image(uri)
        table = backprop.Table(columns=["image"], data=[[first], [second]], log_mode="INCREMENTAL")
        run = self.new()
        run.log({"pending": 7}, commit=False)
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=[
                Response(uri, PNG, [], etag='"first"'), Response(uri, PNG, [], etag='"changed"')]):
            with self.assertRaisesRegex(ValueError, "different reference"):
                run.log({"table": table})
        self.assertEqual((run.step, table._offset), (0, 0))
        self.assertIsNone(first._artifact_target)
        self.assertFalse(any(event["type"] == "history" for event in read_journal(run.sync_dir)))
        with patch("backprop.reference_handlers.runtime.HTTPSession.get", side_effect=self.response):
            run.log({"table": table})
        rows = [event for event in read_journal(run.sync_dir) if event["type"] == "history"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["metrics"]["pending"], 7)
        self.assertEqual(len(rows[0]["payload"]["artifacts"]), 1)
        self.assertEqual(table._offset, 2)


if __name__ == "__main__":
    unittest.main()
