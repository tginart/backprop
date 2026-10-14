"""Selected artifact checksums across local caches and native transport.

Architecture: pinned empty/foo hash vectors -> immutable staged artifact ->
Foundry-shaped upload metadata -> reconstructed entries -> verified downloads
and drafts. Tests use the existing optional xxhash runtime; no packages install.
Main callers: unittest discovery and the SDK release verification suite.
"""
import base64
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.artifact_checksums import bytes_digest, file_checksums
from backprop.artifacts import Artifact, ArtifactManifestEntry, ArtifactDigestAlgorithm
from backprop.transport import MemorySink, Journal, sync

XXH = ArtifactDigestAlgorithm.MANIFEST_XXH128
MD5 = ArtifactDigestAlgorithm.MANIFEST_MD5


class ArtifactChecksumSelectionContracts(unittest.TestCase):
    def test_exact_uppercase_request_requires_optional_package_and_never_falls_back(self):
        with patch.dict(sys.modules, {"xxhash": None}):
            with self.assertRaisesRegex(ImportError, r"xxhash.*backprop-foundry\[checksums\]"):
                Artifact("selected", "model", digest_algorithm="XXH128")
            for requested in ("MD5", "xxh128", "unknown", None):
                with self.subTest(requested=requested):
                    self.assertIs(Artifact("default", "model", digest_algorithm=requested).digest_algorithm, MD5)

    def test_entry_tag_lookup_matches_pinned_uppercase_only_mapping(self):
        for extra, expected in (({}, MD5), ({"alg": "XXH128"}, XXH), ({"alg": "xxh128"}, MD5),
                                ({"alg": None}, MD5), ({"alg": "other"}, MD5)):
            with self.subTest(extra=extra):
                self.assertIs(ArtifactManifestEntry(path="file", digest="", extra=extra).digest_algorithm(), expected)

    def test_enum_import_paths_share_one_identity(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        for path in ("wandb.sdk.artifacts", "wandb.sdk.artifacts.artifact_manifest_entry",
                     "wandb.sdk.artifacts._generated", "wandb.sdk.artifacts._generated.enums"):
            self.assertIs(importlib.import_module(path).ArtifactDigestAlgorithm, ArtifactDigestAlgorithm)


@unittest.skipUnless(importlib.util.find_spec("xxhash"), "Optional xxhash is unavailable")
class ArtifactXXH128Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = MemorySink()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.run = backprop.Run(project="xxh128", entity="tests", dir=self.root,
            settings={"console": "off", "silent": True, "flush_interval": 3600},
            _sink=self.store, _deployment={})

    def tearDown(self):
        self.run.finish()
        self.environment.stop()
        self.temp.cleanup()

    def file(self, name="data.bin", data=b"foo"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def publish(self, artifact):
        self.run.log_artifact(artifact)
        self.run.flush()
        payload = next(event["payload"] for event in reversed(self.store.events)
                       if event["type"] == "artifact" and event["payload"]["artifact_id"] == artifact.id)
        return Artifact._from_manifest(payload, self.store, "tests", "xxh128", self.run.id), payload

    def test_pinned_hashutil_empty_and_foo_vectors(self):
        # W&B v0.30.0 tests/unit_tests/test_lib/test_hashutil.py:150-152.
        for data, expected in ((b"", "maoG0wFHmNhgAcMkRo1Jfw=="), (b"foo", "ea75LoNFQSGrbl9kB359ig==")):
            with self.subTest(data=data):
                self.assertEqual(bytes_digest(data, XXH), expected)
                actual, sha, size = file_checksums(self.file(data=data), XXH)
                self.assertEqual((actual, sha, size), (expected, hashlib.sha256(data).hexdigest(), len(data)))

    def test_upload_preserves_xxh128_and_transport_md5_then_public_downloads(self):
        artifact = Artifact("weights", "model", digest_algorithm="XXH128")
        entry = artifact.add_file(self.file())
        self.assertEqual(entry.digest, "ea75LoNFQSGrbl9kB359ig==")
        self.assertEqual(entry.extra, {"alg": "XXH128"})
        remote, payload = self.publish(artifact)
        reference = payload["files"][0]
        expected_md5 = base64.b64encode(hashlib.md5(b"foo").digest()).decode()
        self.assertEqual(reference["digest"], entry.digest)
        self.assertEqual(reference["md5"], expected_md5)
        self.assertEqual(reference["extra"], {"alg": "XXH128"})
        self.assertEqual(payload["digest_algorithm"], "MANIFEST_XXH128")
        self.assertIs(remote.digest_algorithm, XXH)
        self.assertIs(remote.manifest.digest_algorithm, XXH)
        self.assertIs(remote.get_entry(entry.path).digest_algorithm(), XXH)
        self.assertEqual(Path(remote.file(self.root / "download")).read_bytes(), b"foo")
        self.assertIsNone(remote.verify(self.root / "download"))
        public = remote.files()[0]
        self.assertEqual(public.md5, expected_md5)
        self.assertEqual(public._reference["digest"], entry.digest)
        with public.download(self.root / "public") as downloaded:
            self.assertEqual(downloaded.read(), "foo")
        local_public = artifact.files()[0]
        self.assertEqual(local_public.md5, expected_md5)
        self.assertEqual(local_public._reference["digest"], entry.digest)

    def test_selected_manifest_digest_matches_pinned_header_and_sorted_paths(self):
        import xxhash
        artifact = Artifact("manifest", "dataset", digest_algorithm="XXH128")
        b = artifact.add_file(self.file("b", b"foo"), name="b")
        a = artifact.add_file(self.file("a", b""), name="a")
        expected = xxhash.xxh128(b"wandb-artifact-manifest-v1\n" + f"a:{a.digest}\nb:{b.digest}\n".encode()).hexdigest()
        self.assertEqual(artifact.manifest.digest(), expected)
        remote, _ = self.publish(artifact)
        self.assertEqual(remote.manifest.digest(), expected)
        # Native versions intentionally include metadata under SHA-256.
        self.assertEqual(remote.version, "sha256-" + remote.digest)
        self.assertEqual(len(remote.digest), 64)

    def test_default_md5_native_identity_unchanged_and_empty_algorithms_distinct(self):
        artifact = Artifact("default", "model", metadata={"a": 1})
        entry = artifact.add_file(self.file())
        self.assertEqual(entry.extra, {})
        self.assertEqual(entry.digest, base64.b64encode(hashlib.md5(b"foo").digest()).decode())
        old_identity = {"name": "default", "type": "model", "description": None, "metadata": {"a": 1}, "tags": [],
                        "files": {"data.bin": {"digest": entry.digest, "size": 3, "extra": {}}}}
        expected = hashlib.sha256(json.dumps(old_identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.assertEqual(artifact.digest, expected)
        self.assertEqual(artifact.manifest.digest(), hashlib.md5(b"wandb-artifact-manifest-v1\n" + f"data.bin:{entry.digest}\n".encode()).hexdigest())
        self.assertNotEqual(Artifact("empty", "model").digest, Artifact("empty", "model", digest_algorithm="XXH128").digest)

    def test_cache_validates_selected_hash_before_skip_cache_policy(self):
        artifact = Artifact("cache", "model", digest_algorithm="XXH128")
        artifact.add_file(self.file())
        remote, _ = self.publish(artifact)
        directory = self.root / "cache"
        destination = Path(remote.file(directory))
        with patch.object(self.store, "download_file", side_effect=AssertionError("valid cache must not download")):
            self.assertEqual(remote.file(directory), str(destination))
        destination.write_bytes(b"bar")
        original = self.store.download_file
        with patch.object(self.store, "download_file", wraps=original) as fetch:
            remote.get_entry("data.bin").download(directory)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(destination.read_bytes(), b"foo")
            remote.get_entry("data.bin").download(directory, skip_cache=True)
            self.assertEqual(fetch.call_count, 1)
        destination.write_bytes(b"bar")
        with self.assertRaisesRegex(ValueError, "Digest mismatch"):
            remote.verify(directory)

    def test_entry_verification_uses_xxh128_even_without_sha256(self):
        artifact = Artifact("strict", "model", digest_algorithm="XXH128")
        source = self.file()
        entry = ArtifactManifestEntry(path="data.bin", digest="ea75LoNFQSGrbl9kB359ig==", size=3,
            extra={"alg": "XXH128"}, local_path=str(source), _parent=artifact)
        self.assertEqual(entry._read_verified_bytes(), b"foo")
        source.write_bytes(b"bar")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            entry._read_verified_bytes()

    def test_reconstructed_new_draft_preserves_algorithm_and_checksums(self):
        artifact = Artifact("draft", "model", digest_algorithm="XXH128")
        original = artifact.add_file(self.file())
        remote, _ = self.publish(artifact)
        draft = remote.new_draft()
        self.assertIs(draft.digest_algorithm, XXH)
        self.assertIsNone(draft.id)
        self.assertEqual(draft.get_entry(original.path).digest, original.digest)
        self.assertEqual(draft.get_entry(original.path).extra, {"alg": "XXH128"})
        draft.add_file(self.file("second.bin", b"new"))
        published, _ = self.publish(draft)
        self.assertIs(published.digest_algorithm, XXH)
        self.assertNotEqual(published.version, remote.version)
        self.assertEqual(Path(published.download(self.root / "draft-copy"), "second.bin").read_bytes(), b"new")

    def test_legacy_manifest_without_algorithm_remains_md5(self):
        artifact = Artifact("legacy", "model")
        artifact.add_file(self.file())
        _, payload = self.publish(artifact)
        payload.pop("digest_algorithm")
        remote = Artifact._from_manifest(payload, self.store)
        self.assertIs(remote.digest_algorithm, MD5)
        self.assertEqual(Path(remote.file(self.root / "legacy")).read_bytes(), b"foo")

    def test_offline_sync_retains_algorithm_and_observes_commit(self):
        run = backprop.Run(project="offline", mode="offline", dir=self.root,
            settings={"console": "off", "silent": True}, _deployment={})
        artifact = Artifact("offline", "model", digest_algorithm="XXH128")
        entry = artifact.add_file(self.file())
        run.log_artifact(artifact)
        run.finish()
        self.assertIsNone(artifact.id)
        sync(run.sync_dir, sink=self.store)
        self.assertIsNotNone(artifact.id)
        payload = next(event["payload"] for event in self.store.events if event["type"] == "artifact" and event["payload"]["artifact_id"] == artifact.id)
        self.assertEqual(payload["files"][0]["digest"], entry.digest)
        self.assertEqual(payload["files"][0]["extra"], {"alg": "XXH128"})
        remote = Artifact._from_manifest(payload, self.store)
        self.assertEqual(Path(remote.file(self.root / "offline-download")).read_bytes(), b"foo")


if __name__ == "__main__":
    unittest.main()
