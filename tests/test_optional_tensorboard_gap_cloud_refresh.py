"""Actual provider credential refresh and TLS failures preserve source state.

Architecture: real installed SDK credential flows against loopback TLS ->
native atomic snapshots -> explicit expiry/refresh rejection -> unchanged prior
bytes -> successful retry. Optional real events extend this to durable importer
checkpoints and restart dedup. Main caller: dedicated cloud-client CI.
Why this exists: HTTP transport fixtures with static credentials cannot verify
expiry, OAuth/MSAL refresh, TLS chain/hostname checks or failed-refresh recovery.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backprop.integration.tensorboard import cloud, sources
from tensorboard_gap_cloud_clients import root
from tensorboard_gap_cloud_refresh import RefreshClients, RefreshCloud


def present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


HAS_CLIENTS = all(present(name) for name in ("boto3", "google.cloud.storage", "azure.storage.blob", "azure.identity"))
HAS_TB = all(present(name) for name in ("tensorboard", "numpy", "PIL"))
OUTPUT = Path(os.environ.get("BACKPROP_TENSORBOARD_CLOUD_OUTPUT_DIR",
    str(Path(__file__).resolve().parents[1] / "test-results")))
FILENAME = "events.out.tfevents.1.fixture"


class RequiredRefreshDependencies(unittest.TestCase):
    def test_dedicated_ci_cannot_silently_skip_refresh_or_events(self):
        if os.environ.get("BACKPROP_REQUIRE_TENSORBOARD_CLOUD_CLIENTS") == "1":
            self.assertTrue(HAS_CLIENTS, "aws/gcp/azure SDKs including azure-identity are required")
            self.assertTrue(HAS_TB, "TensorBoard/Pillow/NumPy are required")

    @unittest.skipUnless(present("google.cloud.storage"), "optional GCS SDK is not installed")
    def test_gcs_certificate_rejection_retains_transient_retries_and_source_causes(self):
        from google.auth.exceptions import TransportError
        from google.api_core.exceptions import ServiceUnavailable, TooManyRequests, Forbidden
        from google.cloud.storage.retry import DEFAULT_RETRY
        from requests.exceptions import SSLError
        revised = cloud._gcs_retry()
        for error in (TransportError("transient network failure"), ServiceUnavailable("fixture"),
                      TooManyRequests("fixture"), Forbidden("fixture")):
            self.assertEqual(revised._predicate(error), DEFAULT_RETRY._predicate(error))
        for field in ("_initial", "_maximum", "_multiplier", "_timeout"):
            self.assertEqual(getattr(revised, field), getattr(DEFAULT_RETRY, field))
        certificate = ssl.SSLCertVerificationError("fixture certificate verification")
        wrapped = TransportError(SSLError(certificate))
        self.assertTrue(DEFAULT_RETRY._predicate(wrapped))
        self.assertFalse(revised._predicate(wrapped))
        self.assertIs(wrapped.args[0].args[0], certificate)


@unittest.skipUnless(HAS_CLIENTS, "optional aws/gcp/azure provider clients are not installed")
class TensorBoardCloudRefresh(unittest.TestCase):
    def assert_closed(self, clients):
        for client in clients.clients:
            client.close.assert_called_once_with()

    def test_real_tls_refresh_rotation_denial_expired_reply_and_atomic_resume(self):
        outcomes = []
        for provider in ("s3", "gs", "az"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                initial, appended = b"complete-first-source", b"complete-first-source-appended"
                objects = {"logs/" + FILENAME: initial}
                with RefreshCloud(provider, objects).running() as fixture, \
                        fixture.clock.controlling(provider), RefreshClients(fixture) as clients:
                    run = SimpleNamespace(sync_dir=directory)
                    with patch.object(cloud, "_client", side_effect=clients.client):
                        local, cache = sources.snapshot(run, root(provider) + "/" + FILENAME, root(provider))
                        self.assertEqual(Path(local).read_bytes(), initial)
                        first_generation = len(fixture.issued)
                        self.assertGreaterEqual(first_generation, 1)
                        initial_reads = [r for r in fixture.requests if r.get("purpose") == "source_read"]
                        self.assertTrue(initial_reads)
                        self.assertTrue(all(1 <= r["credential_generation"] <= first_generation for r in initial_reads))
                        fixture.clock.advance()
                        fixture.objects["logs/" + FILENAME] = appended
                        before_rotation = len(fixture.requests)
                        local, _ = sources.snapshot(run, root(provider) + "/" + FILENAME, root(provider))
                        self.assertEqual(Path(local).read_bytes(), appended)
                        rotated_generation = len(fixture.issued)
                        self.assertGreater(rotated_generation, first_generation)
                        rotated_reads = [r for r in fixture.requests[before_rotation:] if r.get("purpose") == "source_read"]
                        self.assertTrue(rotated_reads)
                        self.assertTrue(all(first_generation < r["credential_generation"] <= rotated_generation for r in rotated_reads))
                        fixture.clock.advance()
                        fixture.refresh_denied = True
                        with self.assertRaises(Exception) as denied:
                            sources.snapshot(run, root(provider) + "/" + FILENAME, root(provider))
                        self.assertEqual(type(denied.exception).__name__,
                            {"s3": "ClientError", "gs": "RefreshError", "az": "ClientAuthenticationError"}[provider])
                        if provider == "s3":
                            self.assertEqual(denied.exception.response["Error"]["Code"], "AccessDenied")
                        self.assertEqual(Path(local).read_bytes(), appended)
                        self.assertEqual(len(fixture.issued), rotated_generation)
                        self.assertEqual(list(Path(cache).glob(".tensorboard-copy-*")), [])
                        fixture.refresh_denied = False
                        fixture.issue_expired = True
                        # MSAL intentionally caches failed token replies. Advance
                        # actual cache expiry rather than erase or bypass it.
                        fixture.clock.advance()
                        with self.assertRaises(Exception) as expired:
                            sources.snapshot(run, root(provider) + "/" + FILENAME, root(provider))
                        self.assertEqual(type(expired.exception).__name__,
                            {"s3": "RuntimeError", "gs": "Forbidden", "az": "HttpResponseError"}[provider])
                        self.assertEqual(Path(local).read_bytes(), appended)
                        self.assertEqual(list(Path(cache).glob(".tensorboard-copy-*")), [])
                        fixture.issue_expired = False
                        fixture.clock.advance()
                        resumed = appended + b"-resumed"
                        fixture.objects["logs/" + FILENAME] = resumed
                        local, _ = sources.snapshot(run, root(provider) + "/" + FILENAME, root(provider))
                        self.assertEqual(Path(local).read_bytes(), resumed)
                    self.assert_closed(clients)
                    self.assertEqual(fixture.errors, [])
                    outcomes.append({"provider": provider, "denied_error": type(denied.exception).__name__,
                        "denied_message": str(denied.exception), "expired_error": type(expired.exception).__name__,
                        "expired_message": str(expired.exception), "issued_generations": len(fixture.issued),
                        "snapshot_preserved_on_failure": True, "closed_clients": len(clients.clients)})
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / "tensorboard-gap-cloud-refresh-outcomes.json").write_text(json.dumps(outcomes, indent=2) + "\n")

    def test_actual_clients_reject_untrusted_ca_and_wrong_tls_hostname(self):
        outcomes = []
        for provider in ("s3", "gs", "az"):
            for wrong_hostname, trusted, failure in ((False, False, "untrusted CA"), (True, True, "wrong hostname")):
                with self.subTest(provider=provider, failure=failure), tempfile.TemporaryDirectory() as directory:
                    with RefreshCloud(provider, {"logs/" + FILENAME: b"source"}, label="tls-" + failure.replace(" ", "-"), wrong_hostname=wrong_hostname).running() as fixture, \
                            fixture.clock.controlling(provider), RefreshClients(fixture, trusted=trusted) as clients:
                        with patch.object(cloud, "_client", side_effect=clients.client):
                            with self.assertRaises(Exception) as caught:
                                sources.snapshot(SimpleNamespace(sync_dir=directory), root(provider) + "/" + FILENAME, root(provider))
                        self.assertEqual(type(caught.exception).__name__,
                            {"s3": "SSLError", "gs": "TransportError", "az": "ClientAuthenticationError"}[provider])
                        if trusted:
                            self.assertTrue(any(text in str(caught.exception).lower() for text in ("ip address mismatch", "hostname")))
                        else:
                            self.assertIn("certificate", str(caught.exception).lower())
                        self.assertEqual(fixture.requests, [])
                        self.assertEqual(list(Path(directory).rglob(".tensorboard-copy-*")), [])
                        self.assert_closed(clients)
                        outcomes.append({"provider": provider, "failure": failure,
                                         "error": type(caught.exception).__name__, "message": str(caught.exception)})
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / "tensorboard-gap-cloud-refresh-tls-errors.json").write_text(json.dumps(outcomes, indent=2) + "\n")

    @unittest.skipUnless(HAS_TB, "optional TensorBoard/Pillow/NumPy are not installed")
    def test_actual_events_failed_refresh_preserves_checkpoint_and_restart_dedup(self):
        import backprop
        from backprop.apis.public import Api
        from backprop.integration import tensorboard as tb
        from backprop.transport import read_journal
        from tensorboard_gap_fixtures import events, png
        from tensorboard.summary.writer.event_file_writer import EventFileWriter
        outcomes = []
        settings = {"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True,
                    "flush_interval": 3600, "sync_tensorboard": False}
        for provider in ("s3", "gs", "az"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source"
                writer = EventFileWriter(str(source))
                try:
                    source_events = events()
                    writer.add_event(source_events[0])
                    writer.flush()
                    source_file = next(source.glob("*tfevents*"))
                    initial = source_file.read_bytes()
                    for event in source_events[1:]:
                        writer.add_event(event)
                    writer.flush()
                    appended = source_file.read_bytes()
                finally:
                    writer.close()
                self.assertTrue(appended.startswith(initial))
                key = "logs/train/" + FILENAME
                sink = backprop.MemorySink()
                tb.unpatch()
                with RefreshCloud(provider, {key: initial}, label="refresh-events").running() as fixture, \
                        fixture.clock.controlling(provider), RefreshClients(fixture) as clients, \
                        patch.object(cloud, "_client", side_effect=clients.client), patch.dict(os.environ, {"BACKPROP_DIR": directory}):
                    run = backprop.Run(id="refresh-cloud-" + provider, entity="e", project="tb-gap", dir=directory,
                                       settings=settings, _sink=sink, _deployment={})
                    try:
                        self.assertEqual(tb.sync(run, root(provider), final=True), 1)
                        durable_before = read_journal(run.sync_dir)
                        checkpoint_before = (set(run._tensorboard_seen), set(run._tensorboard_group_seen))
                        snapshots = {p: p.read_bytes() for p in (Path(run.sync_dir) / "tmp" / "tensorboard-sources").rglob("*tfevents*") if p.is_file()}
                        self.assertTrue(snapshots)
                        fixture.objects[key] = appended
                        fixture.capture_sources()
                        fixture.generation += 1
                        fixture.clock.advance()
                        fixture.refresh_denied = True
                        with self.assertRaises(Exception) as caught:
                            tb.sync(run, root(provider), final=True)
                        durable_after = read_journal(run.sync_dir)
                        checkpoint_after = (set(run._tensorboard_seen), set(run._tensorboard_group_seen))
                        diagnostic = {"provider": provider, "error": type(caught.exception).__name__,
                            "before_journal": durable_before, "after_journal": durable_after,
                            "before_record_keys": sorted(checkpoint_before[0]), "after_record_keys": sorted(checkpoint_after[0]),
                            "before_group_revisions": sorted(checkpoint_before[1]), "after_group_revisions": sorted(checkpoint_after[1])}
                        OUTPUT.mkdir(parents=True, exist_ok=True)
                        (OUTPUT / ("tensorboard-gap-cloud-refresh-" + provider + "-checkpoint.json")).write_text(json.dumps(diagnostic, indent=2) + "\n")
                        self.assertEqual(type(caught.exception).__name__,
                            {"s3": "ClientError", "gs": "RefreshError", "az": "ClientAuthenticationError"}[provider])
                        self.assertEqual(durable_after, durable_before)
                        self.assertEqual(checkpoint_after, checkpoint_before)
                        self.assertEqual({p: p.read_bytes() for p in snapshots}, snapshots)
                        self.assertEqual(list(Path(run.sync_dir).rglob(".tensorboard-copy-*")), [])
                        fixture.refresh_denied = False
                        fixture.clock.advance()
                        self.assertEqual(tb.sync(run, root(provider), final=True), 2)
                        self.assertEqual(tb.sync(run, root(provider), final=True), 0)
                        run.finish()
                        rows = list(Api._from_store(sink, entity="e", project="tb-gap").run(run.id).scan_history())
                        self.assertEqual([row["train/loss"] for row in rows], [.25, .125])
                        self.assertEqual(sink.download_file(rows[0]["train/frame"][0]), png())
                        fixture.objects["copied/train/" + FILENAME] = appended
                        with patch.dict(os.environ, {"BACKPROP_DIR": directory + "/restart"}):
                            resumed = backprop.Run(id=run.id, entity="e", project="tb-gap", resume="allow", dir=directory,
                                                  settings=settings, _sink=sink, _deployment={})
                        try:
                            self.assertEqual(tb.sync(resumed, root(provider, "copied"), final=True), 0)
                            resumed.finish()
                        finally:
                            if not resumed._closed:
                                resumed.finish(exit_code=1)
                        self.assert_closed(clients)
                        self.assertEqual(fixture.errors, [])
                        outcomes.append({"provider": provider, "source_sha256": hashlib.sha256(appended).hexdigest(),
                            "denied_error": type(caught.exception).__name__, "checkpoint_unchanged_on_failure": True,
                            "logical_rows": len(rows), "restart_duplicate_rows": 0, "events": sink.events,
                            "credential_requests": fixture.requests})
                    finally:
                        tb.unpatch()
                        if not run._closed:
                            run.finish(exit_code=1)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / "tensorboard-gap-cloud-refresh-events.json").write_text(json.dumps(outcomes, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
