"""Saved notebook discovery without notebook packages or external services.

Architecture: explicit files / VSCode shell / local Jupyter HTTP session server
-> bounded discovery -> real Run metadata and code artifact. A blocked provider
checks that late discovery cannot alter settings after its caller timed out.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop
from backprop.notebook_discovery import resolve_notebook


class NotebookDiscoveryContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.notebook = self.root / "folder" / "training.ipynb"
        self.notebook.parent.mkdir()
        self.notebook.write_text('{"cells":[],"metadata":{},"nbformat":4,"nbformat_minor":5}')
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        backprop.teardown()
        self.addCleanup(backprop.teardown)

    def shell(self, path=None):
        namespace = {} if path is None else {"__vsc_ipynb_file__": str(path)}
        shell = SimpleNamespace(kernel=SimpleNamespace(shell=SimpleNamespace(user_ns=namespace)), config={})
        return patch.dict(sys.modules, {"IPython": SimpleNamespace(get_ipython=lambda: shell)})

    def test_explicit_file_and_environment_override_discovery_even_when_missing(self):
        for value, expected in [(self.notebook, str(self.notebook.resolve())), (self.root / "missing.ipynb", None)]:
            settings = backprop.Settings()
            settings.update_from_env_vars({"WANDB_NOTEBOOK_NAME": str(value)})
            with self.shell(self.notebook), patch("backprop.notebook_discovery._from_servers") as discover:
                resolve_notebook(settings)
            self.assertEqual(settings.notebook_name, str(value))
            self.assertEqual(settings._notebook_path, expected)
            discover.assert_not_called()
            self.assertEqual(bool(settings._notebook_discovery_error), expected is None)

    def test_vscode_discovery_captures_actual_saved_file_in_code_artifact(self):
        sink = backprop.MemorySink()
        with self.shell(self.notebook), patch("backprop.notebook_discovery._from_servers") as discover:
            run = backprop.init(project="notebook-test", dir=self.root, save_code=True, _sink=sink,
                _deployment={}, settings={"console": "off", "silent": True, "disable_git": True})
            run.finish()
        discover.assert_not_called()
        self.assertIsNone(run.settings.notebook_name)
        self.assertEqual(run._settings._notebook_path, str(self.notebook.resolve()))
        artifacts = [event["payload"] for event in sink.events if event["type"] == "artifact"]
        self.assertEqual(len(artifacts), 2)  # Initial capture and final refresh.
        self.assertEqual(artifacts[0]["files"][0]["name"], self.notebook.name)
        self.assertEqual(json.loads(sink.download_file(artifacts[0]["files"][0])), json.loads(self.notebook.read_bytes()))
        metadata = json.loads((Path(run.dir) / "wandb-metadata.json").read_text())
        self.assertEqual(metadata["codePathLocal"], str(self.notebook.resolve()))
        self.assertEqual(metadata["codePath"], self.notebook.name)

    def test_jupyter_kernel_matching_uses_real_sessions_api_and_header_token(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization")))
                payload = [{"kernel": {"id": "other"}, "notebook": {"path": "missing.ipynb"}},
                           {"kernel": {"id": "active"}, "type": "notebook", "path": "folder/training.ipynb"}]
                data = json.dumps(payload).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        record = {"url": f"http://127.0.0.1:{server.server_port}/user/test/", "root_dir": str(self.root), "token": "session-secret"}
        modules = {"ipykernel": SimpleNamespace(connect=SimpleNamespace(get_connection_file=lambda: "/tmp/kernel-active.json")),
                   "jupyter_server.serverapp": SimpleNamespace(list_running_servers=lambda: iter([record])),
                   "notebook.notebookapp": SimpleNamespace(list_running_servers=lambda: iter([]))}
        try:
            settings = backprop.Settings()
            with self.shell(), patch("backprop.notebook_discovery.importlib.import_module", side_effect=lambda name: modules[name]):
                resolve_notebook(settings)
            self.assertEqual(settings._notebook_path, str(self.notebook.resolve()))
            self.assertEqual(settings._notebook_root, str(self.root.resolve()))
            self.assertEqual(calls, [("/user/test/api/sessions", "token session-secret")])
            self.assertNotIn("session-secret", repr(settings.__dict__))
        finally:
            server.shutdown(); worker.join(1); server.server_close()

    def test_legacy_session_shape_and_unavailable_paths_have_safe_diagnostics(self):
        settings = backprop.Settings()
        record = {"url": "https://server.invalid/", "notebook_dir": str(self.root), "token": "private-token"}
        with self.shell(), patch("backprop.notebook_discovery._kernel_and_servers", return_value=("active", [record])):
            with patch("backprop.notebook_discovery._query_sessions", return_value=[{"kernel": {"id": "active"}, "notebook": {"path": "folder/training.ipynb"}}]):
                resolve_notebook(settings)
            self.assertEqual(settings._notebook_path, str(self.notebook.resolve()))
            with patch("backprop.notebook_discovery._query_sessions", side_effect=OSError("https://server.invalid/?token=private-token")):
                resolve_notebook(settings)
            self.assertIsNone(settings._notebook_path)
            self.assertIn("WANDB_NOTEBOOK_NAME", settings._notebook_discovery_error)
            self.assertNotIn("private-token", settings._notebook_discovery_error)
            self.assertNotIn("server.invalid", settings._notebook_discovery_error)

    def test_discovery_deadline_does_not_allow_late_settings_mutation(self):
        release, completed = threading.Event(), threading.Event()
        def blocked(deadline):
            release.wait(2)
            completed.set()
            return self.notebook, self.root
        settings = backprop.Settings()
        try:
            with self.shell(), patch("backprop.notebook_discovery.DISCOVERY_TIMEOUT", 0.03), patch("backprop.notebook_discovery._from_servers", side_effect=blocked):
                start = time.monotonic()
                resolve_notebook(settings)
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertIsNone(settings._notebook_path)
                self.assertIn("time limit", settings._notebook_discovery_error)
        finally:
            release.set()
            self.assertTrue(completed.wait(1))
        self.assertIsNone(settings._notebook_path)

    def test_disabled_code_and_non_notebook_process_do_no_discovery(self):
        with patch("backprop.notebook_discovery._from_servers") as discover:
            with self.shell():
                for settings in [backprop.Settings(disable_code=True), backprop.Settings(mode="disabled")]:
                    resolve_notebook(settings)
                    self.assertIsNone(settings._notebook_path)
            with patch.dict(sys.modules, {"IPython": None}):
                resolve_notebook(backprop.Settings())
        discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
