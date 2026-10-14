"""Distinguish hosted release bytes from the browser authentication boundary.

Architecture: temporary public release fixture -> real HTTP opener against a
loopback server -> verify redirect handling and exact asset comparison. No
credentials or external services are used. Main callers: unittest discovery.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("backprop_verify_release", ROOT / "tools/verify_release.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class ReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        self.responses = {"/index.html": (200, {}, b"release index"),
                          "/app.mjs": (200, {}, b"release module")}
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                owner.requests.append(path)
                status, headers, body = owner.responses.get(path, (404, {}, b"missing"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "deploy").mkdir()
        (self.root / "backend").mkdir()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name, (_, _, body) in self.responses.items():
                bundle.writestr(name.lstrip("/"), body)
        self.archive = buffer.getvalue()
        self.config_path = self.root / "operator.json"
        self.config_path.write_text(json.dumps({
            "host": "https://foundry.example.test",
            "deployment_state": "deploy/state.json",
            "resource_state": "backend/state.json",
        }))
        state = {"archive_sha256": hashlib.sha256(self.archive).hexdigest(), "deployed_version": "test",
                 "host": "https://foundry.example.test", "app_url": "https://backprop.example.test",
                 "application_rid": "ri.third-party-applications.main.application.fixture", "client_id": "test-client"}
        (self.root / "deploy/state.json").write_text(json.dumps(state))
        (self.root / "backend/state.json").write_text(json.dumps({
            "host": state["host"], "app_url": state["app_url"], "branch": "master",
            "stream_dataset_rid": "ri.foundry.main.dataset.stream-fixture",
            "media_dataset_rid": "ri.foundry.main.dataset.media-fixture",
        }))

        class LocalAssetOpener:
            """Keep HTTPS release fixtures isolated to the local test server."""

            def __init__(self):
                self.opener = build_opener(VERIFY._AssetRedirectHandler())

            def open(self, request, timeout):
                target = urlsplit(request.full_url)
                suffix = target.path + ("?" + target.query if target.query else "")
                local = Request(f"http://127.0.0.1:{owner.server.server_port}" + suffix,
                                headers=dict(request.header_items()))
                return self.opener.open(local, timeout=timeout)

        self.opener = LocalAssetOpener()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.temporary.cleanup()

    def verify(self):
        with patch.object(VERIFY, "build_archive", return_value=self.archive), \
                patch.object(VERIFY, "build_opener", return_value=self.opener):
            return VERIFY.verify(self.config_path)

    def test_direct_assets_match_the_release(self):
        self.assertEqual(self.verify()["verified_assets"], 2)
        self.assertEqual(self.requests, ["/index.html", "/app.mjs"])

    def test_anonymous_release_verification_never_loads_a_token(self):
        with patch("foundry_api.operator_token", side_effect=AssertionError("Unexpected credential lookup")) as token:
            self.assertEqual(self.verify()["verified_assets"], 2)
        token.assert_not_called()

    def test_signin_redirect_is_reported_without_following_or_comparing_it(self):
        self.responses["/index.html"] = (302, {"Location": "/multipass/login?state=sensitive-login-state"}, b"")
        self.responses["/multipass/login"] = (200, {}, b"sign-in page")
        with self.assertRaisesRegex(VERIFY.ReleaseAuthenticationRequired, "redirected.*sign-in") as caught:
            self.verify()
        self.assertNotIn("sensitive-login-state", str(caught.exception))
        self.assertEqual(self.requests, ["/index.html"])

    def test_other_redirects_are_not_reported_as_asset_mismatches(self):
        self.responses["/index.html"] = (307, {"Location": "/unclassified-gateway"}, b"")
        with self.assertRaisesRegex(RuntimeError, "redirect before verification"):
            self.verify()
        self.assertEqual(self.requests, ["/index.html"])

    def test_access_denial_identifies_anonymous_verification_limit(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.responses["/index.html"] = (status, {}, b"sensitive response body")
                with self.assertRaisesRegex(VERIFY.ReleaseAuthenticationRequired, f"HTTP {status}") as caught:
                    self.verify()
                self.assertNotIn("sensitive response body", str(caught.exception))

    def test_a_direct_changed_asset_still_fails_exact_byte_comparison(self):
        self.responses["/app.mjs"] = (200, {}, b"old module")
        with self.assertRaisesRegex(ValueError, "does not match the release: app.mjs"):
            self.verify()

    def test_wrong_resource_host_stops_before_archive_or_network(self):
        path = self.root / "backend/state.json"
        resources = json.loads(path.read_text())
        resources["host"] = "https://another-foundry.example.test"
        path.write_text(json.dumps(resources))
        with patch.object(VERIFY, "build_archive") as archive, \
                patch.object(VERIFY, "build_opener") as opener:
            with self.assertRaises(ValueError):
                VERIFY.verify(self.config_path)
        archive.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_wrong_deployment_host_stops_before_archive_or_network(self):
        path = self.root / "deploy/state.json"
        deployment = json.loads(path.read_text())
        deployment["host"] = "https://another-foundry.example.test"
        path.write_text(json.dumps(deployment))
        with patch.object(VERIFY, "build_archive") as archive, \
                patch.object(VERIFY, "build_opener") as opener:
            with self.assertRaises(ValueError):
                VERIFY.verify(self.config_path)
        archive.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
