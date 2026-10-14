"""Prove failure classification through HTTP, journal wrappers and live workers.

Architecture: native HTTP doubles preserve status/cause -> real Run background
threads exercise retries and permanent-stop behavior -> event IDs and journal
acknowledgements prove recovery did not lose or duplicate data. No network access
or installed dependencies are needed. Main callers: unittest discovery.
"""
import errno
import io
import json
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
import warnings

import backprop
from backprop.failures import retryable_failure, retryable_status
from backprop.foundry import FoundryError, FoundryStore
from backprop.transport import BackpropError

CONFIG = {"host": "https://foundry.example.com",
          "stream_dataset_rid": "ri.foundry.main.dataset.events",
          "media_dataset_rid": "ri.foundry.main.dataset.files"}


class FailurePolicyTests(unittest.TestCase):
    def test_http_classes_and_wrapped_network_errors(self):
        for code in (400, 401, 402, 403, 404, 409, 410, 413, 422, 501, 302):
            with self.subTest(code=code):
                self.assertFalse(retryable_status(code))
        for code in (408, 425, 429, 500, 502, 503, 504, 507):
            with self.subTest(code=code):
                self.assertTrue(retryable_status(code))
        for original in (ConnectionError("offline"), TimeoutError("slow"),
                         URLError(OSError(errno.ECONNRESET, "reset")),
                         ssl.SSLEOFError("peer closed"), URLError(ssl.SSLZeroReturnError("TLS closed")),
                         FoundryError("busy", status_code=503, retryable=True)):
            wrapped = BackpropError("Journal preserved")
            wrapped.__cause__ = original
            self.assertTrue(retryable_failure(wrapped))
        for original in (ValueError("checksum"), FileNotFoundError("staged bytes"),
                         URLError(ssl.SSLCertVerificationError("invalid certificate")),
                         FoundryError("denied", status_code=403), RuntimeError("corrupt journal")):
            wrapped = BackpropError("Journal preserved")
            wrapped.__cause__ = original
            self.assertFalse(retryable_failure(wrapped))
        cycle = BackpropError("cycle")
        cycle.__cause__ = cycle
        self.assertFalse(retryable_failure(cycle))

    def test_native_http_retries_preserve_status_and_redact_closed_error_bodies(self):
        store = FoundryStore(CONFIG, "private-test-token")
        for code, attempts in ((503, 4), (500, 4), (429, 4), (403, 1), (501, 1)):
            with self.subTest(code=code):
                bodies = [io.BytesIO(b"private-test-token response") for _ in range(attempts)]
                errors = [HTTPError(CONFIG["host"], code, "failure", {}, body) for body in bodies]
                store.opener = Mock()
                store.opener.open.side_effect = errors
                with patch("backprop.foundry.time.sleep") as sleep, self.assertRaises(FoundryError) as caught:
                    store.request("/test", retry=True)
                self.assertEqual(store.opener.open.call_count, attempts)
                self.assertEqual(sleep.call_count, attempts - 1)
                self.assertEqual(caught.exception.status_code, code)
                self.assertEqual(caught.exception.retryable, attempts > 1)
                self.assertNotIn("private-test-token", str(caught.exception))
                self.assertTrue(all(body.closed for body in bodies))

    def test_certificate_failure_is_not_retried_and_connection_failure_is(self):
        store = FoundryStore(CONFIG, "private-test-token")
        for reason, attempts in ((ssl.SSLCertVerificationError("bad certificate"), 1),
                                 (ConnectionError("temporarily offline"), 4),
                                 (ssl.SSLEOFError("peer closed"), 4),
                                 (ssl.SSLZeroReturnError("TLS closed"), 4)):
            with self.subTest(reason=type(reason).__name__):
                store.opener = Mock()
                store.opener.open.side_effect = URLError(reason)
                with patch("backprop.foundry.time.sleep"), self.assertRaises(FoundryError) as caught:
                    store.request("/test", retry=True)
                self.assertEqual(store.opener.open.call_count, attempts)
                self.assertEqual(caught.exception.retryable, attempts > 1)

    def test_unreadable_error_body_cannot_override_the_http_status(self):
        class SlowBody(io.BytesIO):
            def read(self, *args):
                raise TimeoutError("private-test-token should never appear")
        store = FoundryStore(CONFIG, "private-test-token")
        for code, attempts in ((403, 1), (503, 4)):
            with self.subTest(code=code):
                bodies = [SlowBody() for _ in range(attempts)]
                store.opener = Mock()
                store.opener.open.side_effect = [HTTPError(CONFIG["host"], code, "denied", {}, body) for body in bodies]
                with patch("backprop.foundry.time.sleep"), self.assertRaises(FoundryError) as caught:
                    store.request("/test", retry=True)
                self.assertEqual(caught.exception.status_code, code)
                self.assertEqual(retryable_failure(caught.exception), code == 503)
                self.assertEqual(store.opener.open.call_count, attempts)
                self.assertTrue(all(body.closed for body in bodies))
                self.assertIn("body unavailable", str(caught.exception))
                self.assertNotIn("private-test-token", str(caught.exception))

    def test_transient_failure_does_not_stop_training_when_stop_option_is_enabled(self):
        for problem in (ConnectionError("temporary"),
                        FoundryError("rate limited", status_code=429, retryable=True),
                        FoundryError("unavailable", status_code=503, retryable=True)):
            with self.subTest(problem=type(problem).__name__), tempfile.TemporaryDirectory() as directory:
                sink, stopped, failed = backprop.MemorySink(), threading.Event(), threading.Event()
                settings = backprop.Settings(console="off", quiet=True, flush_interval=.01,
                                             stop_on_fatal_error=True, stop_fn=stopped.set)
                run = backprop.Run(project="retry-test", dir=directory, settings=settings,
                                   _sink=sink, _deployment={})
                original = sink.publish
                attempts = 0
                def publish(events):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        failed.set()
                        raise problem
                    return original(events)
                sink.publish = publish
                try:
                    with warnings.catch_warnings(record=True):
                        run.log({"x": 1})
                        self.assertTrue(failed.wait(1))
                        run.log({"x": 2})
                        deadline = time.monotonic() + 2
                        while sum(event["type"] == "history" for event in sink.events) < 2 and time.monotonic() < deadline:
                            time.sleep(.005)
                    self.assertFalse(stopped.is_set())
                    self.assertEqual(sum(event["type"] == "history" for event in sink.events), 2)
                    self.assertTrue(run._thread.is_alive())
                    self.assertEqual(len(sink.events), len({event["event_id"] for event in sink.events}))
                finally:
                    sink.publish = original
                    run.finish()

    def test_permanent_failure_stops_sending_but_default_mode_still_logs_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            sink, failed = backprop.MemorySink(), threading.Event()
            run = backprop.Run(project="retry-test", dir=directory,
                               settings={"console": "off", "quiet": True, "flush_interval": .01},
                               _sink=sink, _deployment={})
            original = sink.publish
            attempts = 0
            def publish(events):
                nonlocal attempts
                attempts += 1
                failed.set()
                raise FoundryError("access denied", status_code=403)
            sink.publish = publish
            try:
                with warnings.catch_warnings(record=True):
                    run.log({"x": 1})
                    self.assertTrue(failed.wait(1))
                    run._thread.join(timeout=1)
                self.assertFalse(run._thread.is_alive())
                before = run._journal.count
                run.log({"x": 2})
                self.assertGreater(run._journal.count, before)
                self.assertEqual(attempts, 1)
                raw = [json.loads(line) for line in run._journal.event_path.read_text().splitlines()]
                self.assertEqual([row["payload"]["metrics"]["x"] for row in raw if row["type"] == "history"], [1, 2])
                sink.publish = original
                run.flush()
                self.assertEqual(run._journal.ack, run._journal.count)
                self.assertTrue(run._thread.is_alive())
                self.assertEqual(sum(event["type"] == "history" for event in sink.events), 2)
            finally:
                sink.publish = original
                run.finish()


if __name__ == "__main__":
    unittest.main()
