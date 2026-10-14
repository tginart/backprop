"""Real provider-client cloud reads without infrastructure or hosted credentials.

Architecture: portable URL/dependency checks -> loopback protocol fixture with
real provider SDKs -> genuine optional TensorBoard files through native import.
Main callers: focused cloud-client CI and optional test discovery.
Why this exists: protocol fixture success is stronger than replacing a provider,
but still does not prove real accounts, OAuth refresh, IAM or hosted TLS access.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from backprop.integration.tensorboard import cloud, sources, _path
from tensorboard_gap_cloud_clients import ProtocolCloud, provider_client, root


def present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


PROVIDERS = {"s3": present("boto3"), "gs": present("google.cloud.storage"), "az": present("azure.storage.blob")}
HAS_CLIENTS = all(PROVIDERS.values())
HAS_TB = present("tensorboard") and present("PIL") and present("numpy")
OUTPUT = Path(os.environ.get("BACKPROP_TENSORBOARD_CLOUD_OUTPUT_DIR",
                             str(Path(__file__).resolve().parents[1] / "test-results"))) / "tensorboard-gap-cloud-clients.json"


class TensorBoardCloudClientPortable(unittest.TestCase):
    def test_required_ci_dependencies_do_not_silently_skip(self):
        if os.environ.get("BACKPROP_REQUIRE_TENSORBOARD_CLOUD_CLIENTS") == "1":
            self.assertTrue(HAS_CLIENTS, PROVIDERS)
            self.assertTrue(HAS_TB, "TensorBoard/Pillow/NumPy are required in the dedicated provider profile")

    def test_supported_addresses_and_explicit_unsupported_options(self):
        for scheme in ("s3", "gs", "az"):
            for prefix in ("", "logs", "logs/train", "logs/literal%2Fname"):
                value = root(scheme, prefix)
                parsed = cloud.address(value)
                self.assertEqual(parsed.key, prefix)
                self.assertEqual(parsed.bucket, "fixture")
                self.assertEqual(cloud.address(_path(value)).key, prefix)
        for value in ("s3:///logs", "s3://user:pass@fixture/logs", "gs://fixture:443/logs", "ftp://fixture/logs",
                      "s3://fixture/logs?endpoint=x", "gs://fixture/logs#fragment", "az://fixture",
                      "az://fixture/fixture/../logs", "s3://fixture/logs//train", "s3://fixture/logs\0bad",
                      "s3://fixture//logs", "s3://fixture//", "gs://fixture/logs//", "az://fixture//fixture/logs",
                      "s3://fixture/logs/C:/outside", "s3://fixture/logs/C:\\outside\\events.out.tfevents.fixture"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _path(value)

    def test_cloud_snapshot_names_cannot_escape_a_windows_cache(self):
        cache = PureWindowsPath("C:/run/tmp/tensorboard-sources/cache")
        for relative in ("C:\\outside\\events.out.tfevents.fixture", "C:/outside/events.out.tfevents.fixture",
                         "C:events.out.tfevents.fixture", "train/C:/events.out.tfevents.fixture",
                         "..\\outside\\events.out.tfevents.fixture", "\\\\server\\share\\events.out.tfevents.fixture"):
            with self.subTest(relative=relative):
                with self.assertRaises(ValueError):
                    sources.relative_source("s3://fixture/logs/" + relative, "s3://fixture/logs")
                with self.assertRaises(ValueError):
                    cloud.address("s3://fixture/logs/" + relative)
        safe = sources.relative_source("s3://fixture/logs/train/literal%2Fname", "s3://fixture/logs")
        self.assertEqual((cache / safe).relative_to(cache), PureWindowsPath("train/literal%2Fname"))

    def test_invalid_cloud_root_does_not_mutate_patch_session(self):
        from backprop.integration import tensorboard as tb
        tb.unpatch()
        try:
            with patch.object(tb.importlib.util, "find_spec", return_value=SimpleNamespace()):
                with self.assertRaisesRegex(ValueError, "query and fragment"):
                    tb.patch(save=False, root_logdir="s3://fixture/logs?endpoint=ignored")
            self.assertFalse(tb.is_patched())
            self.assertTrue(tb._save)
            self.assertEqual(tb._root_logdir, "")
        finally:
            tb.unpatch()

    def test_missing_dependencies_and_broken_installed_clients_are_explicit(self):
        for name, extra in (("boto3", "aws"), ("google.cloud.storage", "gcp"), ("azure.storage.blob", "azure")):
            missing = ModuleNotFoundError("fixture missing", name=name.split(".")[0])
            with patch.object(cloud.importlib, "import_module", side_effect=missing):
                with self.assertRaisesRegex(ImportError, "backprop-foundry\\[" + extra + "\\]"):
                    cloud._dependency(name, extra)
            broken = ModuleNotFoundError("broken installed SDK", name="broken_transitive_dependency")
            with patch.object(cloud.importlib, "import_module", side_effect=broken):
                with self.assertRaises(ModuleNotFoundError) as failure:
                    cloud._dependency(name, extra)
                self.assertIs(failure.exception, broken)

    def test_listing_and_download_operations_release_owned_clients_on_error(self):
        client = SimpleNamespace(close=Mock(), _tensorboard_source_credential=SimpleNamespace(close=Mock()))
        with patch.object(cloud, "_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "fixture error"):
                with sources.open_filesystem(root("s3")):
                    raise RuntimeError("fixture error")
        client.close.assert_called_once_with()
        client._tensorboard_source_credential.close.assert_called_once_with()

    def test_azure_invalid_endpoint_options_precede_credential_construction(self):
        sdk, identity = SimpleNamespace(BlobServiceClient=Mock()), SimpleNamespace(DefaultAzureCredential=Mock())
        with patch.object(cloud, "_dependency", side_effect=lambda name, extra: identity if name == "azure.identity" else sdk):
            for environment in ({"AZURE_STORAGE_PROTOCOL": "ftp"}, {"AZURE_STORAGE_IS_LOCAL_EMULATOR": "invalid"},
                                {"AZURE_STORAGE_IS_CDN": "invalid"}):
                with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True):
                    with self.assertRaises(ValueError):
                        cloud._azure_client(cloud.address(root("az")))
        identity.DefaultAzureCredential.assert_not_called()
        sdk.BlobServiceClient.assert_not_called()

    def test_azure_connection_account_mismatch_closes_constructed_client(self):
        client = SimpleNamespace(account_name="different", url="https://different.blob.core.windows.net/", close=Mock())
        sdk = SimpleNamespace(BlobServiceClient=Mock())
        sdk.BlobServiceClient.from_connection_string.return_value = client
        with patch.object(cloud, "_dependency", return_value=sdk), patch.dict(os.environ,
                {"AZURE_STORAGE_CONNECTION_STRING": "AccountName=different;AccountKey=fixture"}, clear=True):
            with self.assertRaisesRegex(ValueError, "account disagrees with the connection string"):
                cloud._azure_client(cloud.address(root("az")))
        client.close.assert_called_once_with()

    @unittest.skipUnless(PROVIDERS["az"], "optional azure-storage-blob is not installed")
    def test_actual_azure_connection_endpoint_account_is_checked_before_requests(self):
        from azure.storage.blob import BlobServiceClient
        # Genuine SDK construction and URL/account parsing, without HTTP I/O.
        # The SDK replaces account_name with the credential's AccountName, so
        # the real endpoint must be checked separately and closed on rejection.
        for endpoint in ("https://different.blob.core.windows.net", "http://127.0.0.1:10000/different",
                         "http://localhost:10000/different"):
            connection = "AccountName=fixture;AccountKey=Zml4dHVyZQ==;BlobEndpoint=" + endpoint
            client = BlobServiceClient.from_connection_string(connection)
            self.assertEqual(client.account_name, "fixture")
            self.assertEqual(client.url.rstrip("/"), endpoint)
            sdk = SimpleNamespace(BlobServiceClient=Mock())
            sdk.BlobServiceClient.from_connection_string.return_value = client
            with self.subTest(endpoint=endpoint), patch.object(cloud, "_dependency", return_value=sdk), \
                    patch.dict(os.environ, {"AZURE_STORAGE_CONNECTION_STRING": connection}, clear=True), \
                    patch.object(client, "close", wraps=client.close) as close:
                with self.assertRaisesRegex(ValueError, "account disagrees with the connection string"):
                    cloud._azure_client(cloud.address(root("az")))
                close.assert_called_once_with()
        for endpoint in ("https://fixture.blob.core.windows.net", "https://fixture-secondary.blob.core.windows.net",
                         "https://fixture.blob.core.usgovcloudapi.net", "https://fixture.blob.core.chinacloudapi.cn",
                         "https://fixture.blob.core.cloudapi.de", "http://127.0.0.1:10000/fixture",
                         "http://localhost:10000/fixture", "https://custom.fixture.invalid"):
            connection = "AccountName=fixture;AccountKey=Zml4dHVyZQ==;BlobEndpoint=" + endpoint
            with self.subTest(endpoint=endpoint), patch.dict(os.environ,
                    {"AZURE_STORAGE_CONNECTION_STRING": connection}, clear=True):
                client = cloud._azure_client(cloud.address(root("az")))
                self.assertEqual(client.url.rstrip("/"), endpoint)
                client.close()

    @unittest.skipUnless(PROVIDERS["az"], "optional azure-storage-blob is not installed")
    def test_actual_azure_key_then_sas_then_connection_precedence(self):
        connection = "AccountName=fixture;AccountKey=Zml4dHVyZQ==;BlobEndpoint=https://custom.fixture.invalid"
        sas = "sv=2025-11-05&ss=b&srt=o&sp=r&sig=fixture"
        shared = {"AZURE_STORAGE_ACCOUNT": "fixture", "AZURE_STORAGE_CONNECTION_STRING": connection}
        cases = (({**shared, "AZURE_STORAGE_SAS_TOKEN": sas, "AZURE_STORAGE_KEY": "Zml4dHVyZQ=="}, "key"),
                 ({**shared, "AZURE_STORAGE_SAS_TOKEN": sas}, "sas"), (shared, "connection"))
        for environment, selected in cases:
            with self.subTest(selected=selected), patch.dict(os.environ, environment, clear=True):
                client = cloud._azure_client(cloud.address(root("az")))
                try:
                    endpoint = urlsplit(client.url)
                    if selected == "sas":
                        self.assertEqual(endpoint.hostname, "fixture.blob.core.windows.net")
                        self.assertEqual(parse_qs(endpoint.query), parse_qs(sas))
                        self.assertIsNone(client.credential)
                    else:
                        self.assertEqual(endpoint.hostname, "custom.fixture.invalid" if selected == "connection"
                                         else "fixture.blob.core.windows.net")
                        self.assertEqual(endpoint.query, "")
                        self.assertEqual(client.credential.account_name, "fixture")
                finally:
                    client.close()

    def test_azure_failed_client_construction_closes_owned_credential(self):
        credential = SimpleNamespace(close=Mock())
        sdk = SimpleNamespace(BlobServiceClient=Mock(side_effect=RuntimeError("fixture construction error")))
        identity = SimpleNamespace(DefaultAzureCredential=Mock(return_value=credential))
        with patch.object(cloud, "_dependency", side_effect=lambda name, extra: identity if name == "azure.identity" else sdk):
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "fixture construction"):
                cloud._azure_client(cloud.address(root("az")))
        credential.close.assert_called_once_with()

    @unittest.skipUnless(PROVIDERS["az"], "optional azure-storage-blob is not installed")
    def test_azure_account_mismatch_fails_without_credential_discovery(self):
        with patch.dict(os.environ, {"AZURE_STORAGE_ACCOUNT": "different"}):
            with self.assertRaisesRegex(ValueError, "account disagrees"):
                cloud._azure_client(cloud.address(root("az")))


@unittest.skipUnless(HAS_CLIENTS, "optional aws/gcp/azure provider clients are not installed")
class TensorBoardCloudClientProtocols(unittest.TestCase):
    def test_actual_signing_paginated_nested_listing_binary_streams_and_denial(self):
        data = bytes(range(251)) * 9000
        outcomes = []
        for provider in ("s3", "gs", "az"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                objects = {"logs/train/events.out.tfevents.1.fixture": data,
                           "logs/validation/events.out.tfevents.2.fixture": b"validation",
                           "logs/train/literal%2Fname": b"literal"}
                with ProtocolCloud(provider, objects).running() as fixture:
                    with patch.object(cloud, "_client", side_effect=lambda source: provider_client(fixture)):
                        filesystem = sources.filesystem(root(provider))
                        self.addCleanup(filesystem.close)
                        self.assertTrue(filesystem.isdir(root(provider)))
                        tree = list(filesystem.walk(root(provider)))
                        self.assertEqual(tree[0][1], ["train", "validation"])
                        self.assertEqual(sorted(name for _, _, names in tree for name in names),
                                         ["events.out.tfevents.1.fixture", "events.out.tfevents.2.fixture", "literal%2Fname"])
                        with sources.open_filesystem(root(provider, "")) as bucket_filesystem:
                            self.assertTrue(bucket_filesystem.isdir(root(provider, "")))
                        run = SimpleNamespace(sync_dir=directory)
                        source = root(provider) + "/train/events.out.tfevents.1.fixture"
                        local, cache = sources.snapshot(run, source, root(provider))
                        self.assertEqual(Path(local).read_bytes(), data)
                        literal, _ = sources.snapshot(run, root(provider) + "/train/literal%2Fname", root(provider))
                        self.assertEqual(Path(literal).read_bytes(), b"literal")
                        with self.assertRaisesRegex(ValueError, "binary source reads"):
                            with filesystem.GFile(source, "wb"):
                                self.fail("Cloud source writes must be rejected")
                        from botocore.exceptions import ClientError
                        from azure.core.exceptions import ResourceNotFoundError
                        missing = {"s3": ClientError, "gs": FileNotFoundError, "az": ResourceNotFoundError}[provider]
                        with self.assertRaises(missing):
                            sources.snapshot(run, root(provider) + "/missing", root(provider))
                        fixture.deny_reads = True
                        from botocore.exceptions import ClientError
                        from google.api_core.exceptions import Forbidden
                        from azure.core.exceptions import HttpResponseError
                        expected_error = {"s3": ClientError, "gs": Forbidden, "az": HttpResponseError}[provider]
                        with self.assertRaises(expected_error):
                            sources.snapshot(run, source, root(provider))
                        self.assertEqual(Path(local).read_bytes(), data)
                        self.assertFalse(list(Path(cache).rglob(".tensorboard-copy-*")))
                        fixture.denied = True
                        with self.assertRaises(expected_error):
                            list(filesystem.walk(root(provider)))
                    self.assertFalse(fixture.errors, fixture.errors)
                    self.assertTrue(all(request["authentication"] in {"AWS4-HMAC-SHA256", "Bearer", "SharedKey"}
                                        for request in fixture.requests))
                    self.assertTrue(any("continuation-token=" in request["path"] or "pageToken=" in request["path"]
                                        or "marker=" in request["path"] for request in fixture.requests))
                    if provider == "gs":
                        self.assertTrue(any("ifGenerationMatch=1" in request["path"] for request in fixture.requests))
                    if provider == "az":
                        self.assertTrue(any(request["if_match"] == '"fixture-1"' for request in fixture.requests))
                    outcomes.append({"provider": provider, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                     "source_preserved_after_denial": True, "requests": fixture.requests})
        OUTPUT.parent.mkdir(exist_ok=True)
        OUTPUT.write_text(json.dumps({"fixture": "loopback HTTP with synthetic credentials", "hosted_authentication": "unverified",
                                      "outcomes": outcomes}, indent=2) + "\n")

    def test_gcs_full_object_md5_detects_corruption_before_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory, ProtocolCloud("gs", {"logs/events.out.tfevents.1.fixture": b"complete"}, label="checksum").running() as fixture:
            run = SimpleNamespace(sync_dir=directory)
            source = root("gs") + "/events.out.tfevents.1.fixture"
            with patch.object(cloud, "_client", side_effect=lambda source: provider_client(fixture)):
                local, cache = sources.snapshot(run, source, root("gs"))
                fixture.objects["logs/events.out.tfevents.1.fixture"] = b"corrupted"
                fixture.generation += 1
                fixture.bad_md5 = True
                with self.assertRaisesRegex(OSError, "MD5 checksum mismatch"):
                    sources.snapshot(run, source, root("gs"))
                self.assertEqual(Path(local).read_bytes(), b"complete")
                self.assertFalse(list(Path(cache).rglob(".tensorboard-copy-*")))
            self.assertFalse(fixture.errors, fixture.errors)

    def test_s3_interrupted_http_stream_preserves_last_snapshot(self):
        from botocore.exceptions import ResponseStreamingError
        with tempfile.TemporaryDirectory() as directory, ProtocolCloud("s3", {"logs/events.out.tfevents.1.fixture": b"complete" * 1000}, label="interrupted").running() as fixture:
            run = SimpleNamespace(sync_dir=directory)
            source = root("s3") + "/events.out.tfevents.1.fixture"
            with patch.object(cloud, "_client", side_effect=lambda source: provider_client(fixture)):
                local, cache = sources.snapshot(run, source, root("s3"))
                fixture.objects["logs/events.out.tfevents.1.fixture"] = b"interrupted" * 1000
                fixture.truncate = True
                with self.assertRaises(ResponseStreamingError):
                    sources.snapshot(run, source, root("s3"))
                self.assertEqual(Path(local).read_bytes(), b"complete" * 1000)
                self.assertFalse(list(Path(cache).rglob(".tensorboard-copy-*")))
            self.assertFalse(fixture.errors, fixture.errors)

    @unittest.skipUnless(HAS_TB, "optional TensorBoard/Pillow/NumPy are not installed")
    def test_genuine_events_growing_cloud_source_restart_and_copied_root_identity(self):
        import backprop
        from backprop.apis.public import Api
        from backprop.integration import tensorboard as tb
        from tensorboard_gap_fixtures import events, png
        from tensorboard.summary.writer.event_file_writer import EventFileWriter
        settings = {"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True, "flush_interval": 3600}
        outcomes = []
        for provider in ("s3", "gs", "az"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                source_events = events()
                writer = EventFileWriter(str(Path(directory) / "source"))
                try:
                    writer.add_event(source_events[0])
                    writer.flush()
                    source_file = next((Path(directory) / "source").glob("*tfevents*"))
                    initial = source_file.read_bytes()
                    for event in source_events[1:]:
                        writer.add_event(event)
                    writer.flush()
                    full = source_file.read_bytes()
                finally:
                    writer.close()
                self.assertTrue(full.startswith(initial))
                key = "logs/train/events.out.tfevents.1.fixture"
                sink = backprop.MemorySink()
                with ProtocolCloud(provider, {key: initial}, label="events").running() as fixture:
                    with patch.object(cloud, "_client", side_effect=lambda source: provider_client(fixture)), patch.dict(os.environ, {"BACKPROP_DIR": directory}):
                        run = backprop.Run(id="cloud-client-" + provider, entity="e", project="tb-gap", dir=directory,
                                           settings=settings, _sink=sink, _deployment={})
                        try:
                            self.assertEqual(tb.sync(run, root(provider), final=True), 1)
                            fixture.objects[key] = full
                            fixture.capture_sources()
                            fixture.generation += 1
                            # Closing the historical prefix changes its barrier
                            # evidence and adds the next row: two physical group
                            # revisions still project to exactly two source rows.
                            self.assertEqual(tb.sync(run, root(provider), final=True), 2)
                            self.assertEqual(tb.sync(run, root(provider), final=True), 0)
                            run.finish()
                        finally:
                            if not run._closed:
                                run.finish(exit_code=1)
                        api = Api._from_store(sink, entity="e", project="tb-gap")
                        rows = list(api.run(run.id).scan_history())
                        self.assertEqual([row["train/loss"] for row in rows], [.25, .125])
                        self.assertEqual(sink.download_file(rows[0]["train/frame"][0]), png())
                        fixture.objects["copied/train/events.out.tfevents.1.fixture"] = full
                        with patch.dict(os.environ, {"BACKPROP_DIR": directory + "/restart"}):
                            resumed = backprop.Run(id=run.id, entity="e", project="tb-gap", resume="allow", dir=directory,
                                                   settings=settings, _sink=sink, _deployment={})
                        try:
                            self.assertEqual(tb.sync(resumed, root(provider, "copied"), final=True), 0)
                            resumed.finish()
                        finally:
                            if not resumed._closed:
                                resumed.finish(exit_code=1)
                        outcomes.append({"provider": provider, "source_sha256": hashlib.sha256(full).hexdigest(),
                                         "logical_rows": len(rows), "events": sink.events, "requests": fixture.requests})
                    self.assertFalse(fixture.errors, fixture.errors)
        target = OUTPUT.with_name("tensorboard-gap-cloud-clients-events.json")
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps({"hosted_authentication": "unverified", "outcomes": outcomes}, indent=2) + "\n")

    @unittest.skipUnless(HAS_TB, "optional TensorBoard/Pillow/NumPy are not installed")
    def test_genuine_public_api_import_ignores_manual_cloud_save_false_policy(self):
        import backprop
        from backprop.apis.public import Api
        from backprop.integration import tensorboard as tb
        from tensorboard_gap_fixtures import events, png, write
        for provider in ("s3", "gs", "az"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                source = write(Path(directory) / "source", events()[:1]).read_bytes()
                sink = backprop.MemorySink()
                tb.unpatch()
                with ProtocolCloud(provider, {"logs/train/events.out.tfevents.1.fixture": source}, label="public-api").running() as fixture:
                    with patch.object(cloud, "_client", side_effect=lambda source: provider_client(fixture)), patch.dict(os.environ, {"BACKPROP_DIR": directory}):
                        run = backprop.Run(id="manual-cloud-" + provider, entity="e", project="tb-gap", dir=directory,
                                           settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True},
                                           _sink=sink, _deployment={})
                        try:
                            tb.patch(save=False, root_logdir=root(provider))
                            self.assertEqual(tb.sync(run, root(provider), final=True), 1)
                            run.finish()
                            self.assertTrue(tb.is_patched())
                            self.assertFalse(any(event["type"] == "file" and "tfevents" in event["payload"]["name"]
                                                 for event in sink.events))
                            api = Api._from_store(sink, entity="e", project="tb-gap")
                            result = api.sync_tensorboard(root(provider), run_id="public-cloud-" + provider,
                                                          project="tb-gap", entity="e")
                            rows = list(result.scan_history())
                            self.assertEqual(len(rows), 1)
                            self.assertEqual(rows[0]["train/loss"], .25)
                            self.assertEqual(sink.download_file(rows[0]["train/frame"][0]), png())
                            saved = [event["payload"]["file"] for event in sink.events if event["type"] == "file"
                                     and "tfevents" in event["payload"]["name"]]
                            self.assertEqual(len(saved), 1)
                            self.assertEqual(sink.download_file(saved[0]), source)
                        finally:
                            tb.unpatch()
                            if not run._closed:
                                run.finish(exit_code=1)
                        self.assertFalse(tb.is_patched())
                    self.assertFalse(fixture.errors, fixture.errors)


if __name__ == "__main__":
    unittest.main()
