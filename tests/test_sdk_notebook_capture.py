"""Notebook lifecycle through real Run journals and IPython-compatible fixtures.

Architecture: history/display/event providers -> notebook callbacks -> immutable
code artifacts and run files. Fixtures exercise the provider contracts without
installing notebook packages; an optional real InteractiveShell test runs only
when that runtime already exists. Blocked providers prove late work cannot write.
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop
from backprop.notebook_capture import NotebookCapture


class Events:
    def __init__(self):
        self.callbacks = {"post_run_cell": []}
    def register(self, name, callback):
        self.callbacks[name].append(callback)
    def unregister(self, name, callback):
        self.callbacks[name].remove(callback)
    def trigger(self, name):
        for callback in list(self.callbacks[name]):
            callback(SimpleNamespace(success=True))


class Publisher:
    def __init__(self):
        self.calls = []
    def publish(self, data, metadata=None, source=None, **kwargs):
        self.calls.append((data, metadata, source, kwargs))
        return "published"


class NotebookCaptureContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "training.ipynb"
        self.write_notebook("initial = 1")
        self.sink = backprop.MemorySink()
        self.rows = [(1, 1, ("value = 2", None)), (1, 2, ("value + 3", "5"))]
        self.shell = SimpleNamespace(events=Events(), display_pub=Publisher(), execution_count=2,
            history_manager=SimpleNamespace(get_range=lambda output: iter(self.rows)),
            kernel=SimpleNamespace(language_info={"name": "python", "version": "3.12"},
                shell=SimpleNamespace(user_ns={"__vsc_ipynb_file__": str(self.path)})), config={})
        self.original_publish = self.shell.display_pub.publish
        self.runs = []
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start(); self.addCleanup(environment.stop)
        shell = patch.dict(sys.modules, {"IPython": SimpleNamespace(get_ipython=lambda: self.shell)})
        shell.start(); self.addCleanup(shell.stop)
        self.addCleanup(self.cleanup_runs)

    def cleanup_runs(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        backprop.teardown()

    def write_notebook(self, source):
        self.path.write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5,
            "metadata": {}, "cells": [{"cell_type": "code", "source": source,
                "metadata": {}, "execution_count": 1, "outputs": [], "id": "source-cell"}]}))

    def new(self, **settings):
        run = backprop.Run(project="notebook-capture", dir=self.root, _sink=self.sink, _deployment={},
            settings={"console": "off", "silent": True, "save_code": True, "disable_git": True, **settings})
        self.runs.append(run)
        return run

    def manifests(self, run):
        return [event["payload"] for event in self.sink.events if event["type"] == "artifact" and event["run_id"] == run.id]

    def test_cell_refresh_and_finish_preserve_history_text_and_rich_outputs(self):
        run = self.new()
        result = self.shell.display_pub.publish({"text/plain": "shown", "image/png": b"binary-image"}, {"width": 12}, "fixture", update=True)
        self.assertEqual(result, "published")
        self.write_notebook("changed = 2")
        self.shell.events.trigger("post_run_cell")
        run.flush()
        manifest = self.manifests(run)[-1]
        current = json.loads(self.sink.download_file(manifest["files"][0]))
        self.assertEqual(current["cells"][0]["source"], "changed = 2")
        self.write_notebook("final = 3")
        run.finish()
        history_path = Path(run.dir) / "code/_session_history.ipynb"
        history = json.loads(history_path.read_text())
        self.assertEqual([cell["source"] for cell in history["cells"]], ["value = 2", "value + 3"])
        self.assertEqual([cell["execution_count"] for cell in history["cells"]], [1, 2])
        self.assertEqual(history["cells"][1]["outputs"][0], {"output_type": "stream", "name": "stdout", "text": "5"})
        display = history["cells"][1]["outputs"][1]
        self.assertEqual(display["data"]["image/png"], base64.b64encode(b"binary-image").decode())
        self.assertEqual(display["metadata"], {"width": 12})
        self.assertEqual(run.config["_wandb"]["session_history"], "code/_session_history.ipynb")
        files = {entry["name"]: entry for entry in self.manifests(run)[-1]["files"]}
        self.assertEqual(set(files), {"training.ipynb", "_session_history.ipynb"})
        self.assertEqual(json.loads(self.sink.download_file(files["training.ipynb"]))["cells"][0]["source"], "final = 3")
        self.assertTrue(any(event["type"] == "file" and event["payload"]["name"] == "code/_session_history.ipynb" for event in self.sink.events))
        self.assertEqual(self.shell.events.callbacks["post_run_cell"], [])
        self.assertEqual(self.shell.display_pub.publish, self.original_publish)

    def test_credentials_are_removed_from_source_history_display_and_snapshots(self):
        run = self.new()
        os.environ["EXAMPLE_API_TOKEN"] = "environment-secret"
        source = 'api_key = "literal-secret"\nprint("environment-secret")\nconfig = {"api_key": "dict-literal-secret"}\nos.environ["OTHER_TOKEN"] = "assigned-secret"'
        self.write_notebook(source)
        self.rows[0] = (1, 1, (source, "environment-secret"))
        self.shell.display_pub.publish({"text/plain": "Authorization: Bearer header-secret"}, {"api_key": "metadata-secret"})
        run.finish()
        self.assertIn("literal-secret", self.path.read_text())
        payloads = b"\n".join(self.sink.files.values())
        for secret in [b"environment-secret", b"literal-secret", b"header-secret", b"metadata-secret", b"dict-literal-secret", b"assigned-secret"]:
            self.assertNotIn(secret, payloads)
        self.assertIn(b"[redacted]", payloads)

    def test_missing_saved_notebook_does_not_discard_available_session_history(self):
        run = self.new()
        self.path.unlink()
        run.finish()
        self.assertTrue(any("FileNotFoundError" in warning for warning in run._warnings))
        history = json.loads((Path(run.dir) / "code/_session_history.ipynb").read_text())
        self.assertEqual(len(history["cells"]), 2)
        self.assertEqual([entry["name"] for entry in self.manifests(run)[-1]["files"]], ["_session_history.ipynb"])

    def test_overlapping_runs_finish_in_any_order_without_losing_publisher(self):
        first, second = self.new(), self.new()
        self.assertEqual(len(self.shell.events.callbacks["post_run_cell"]), 2)
        self.shell.display_pub.publish({"text/plain": "both"})
        self.assertEqual(len(self.shell.display_pub.calls), 1)
        first.finish()
        self.assertEqual(len(self.shell.events.callbacks["post_run_cell"]), 1)
        self.assertNotEqual(self.shell.display_pub.publish, self.original_publish)
        self.shell.display_pub.publish({"text/plain": "second-only"})
        second.finish()
        first_history = json.loads((Path(first.dir) / "code/_session_history.ipynb").read_text())
        second_history = json.loads((Path(second.dir) / "code/_session_history.ipynb").read_text())
        self.assertNotIn("second-only", json.dumps(first_history))
        self.assertIn("second-only", json.dumps(second_history))
        self.assertEqual(self.shell.display_pub.publish, self.original_publish)

    def test_timeout_detaches_immediately_and_late_history_never_writes(self):
        entered, release, complete = threading.Event(), threading.Event(), threading.Event()
        def blocked(output):
            entered.set(); release.wait(2); complete.set()
            return iter(self.rows)
        self.shell.history_manager.get_range = blocked
        run = self.new()
        try:
            with patch("backprop.notebook_capture.CAPTURE_TIMEOUT", 0.03):
                start = time.monotonic(); run.finish()
                self.assertLess(time.monotonic() - start, 0.5)
            self.assertTrue(entered.is_set())
            self.assertTrue(any("deadline" in warning for warning in run._warnings))
            self.assertEqual(self.shell.events.callbacks["post_run_cell"], [])
            self.assertEqual(self.shell.display_pub.publish, self.original_publish)
            before = list(self.sink.events)
        finally:
            release.set(); self.assertTrue(complete.wait(1))
        self.assertEqual(self.sink.events, before)
        self.assertFalse((Path(run.dir) / "code/_session_history.ipynb").exists())

    def test_history_limit_warns_without_publishing_truncated_history(self):
        run = self.new()
        with patch("backprop.notebook_snapshot.MAX_HISTORY_CELLS", 1):
            run.finish()
        self.assertTrue(any("size/cell limit" in warning for warning in run._warnings))
        self.assertFalse((Path(run.dir) / "code/_session_history.ipynb").exists())
        self.assertEqual(self.shell.events.callbacks["post_run_cell"], [])

    def test_disabled_capture_and_failed_init_leave_no_callbacks(self):
        for settings in [{"save_code": False}, {"disable_code": True}, {"mode": "disabled"}]:
            self.new(**settings).finish()
            self.assertEqual(self.shell.events.callbacks["post_run_cell"], [])
            self.assertEqual(self.shell.display_pub.publish, self.original_publish)
        original = NotebookCapture.start
        def fail_after_registration(capture):
            original(capture)
            raise RuntimeError("Failed later initialization")
        with patch.object(NotebookCapture, "start", fail_after_registration):
            with self.assertRaises(RuntimeError):
                self.new()
        self.assertEqual(self.shell.events.callbacks["post_run_cell"], [])
        self.assertEqual(self.shell.display_pub.publish, self.original_publish)


@unittest.skipUnless(importlib.util.find_spec("IPython") is not None, "A real IPython runtime is not installed")
class RealIPythonNotebookCapture(unittest.TestCase):
    def test_real_history_manager_and_post_cell_event(self):
        from IPython.core.interactiveshell import InteractiveShell
        from traitlets.config import Config
        import IPython
        with tempfile.TemporaryDirectory() as directory:
            config = Config(); config.HistoryManager.hist_file = ":memory:"
            shell = InteractiveShell(config=config, ipython_dir=directory)
            shell.kernel = SimpleNamespace(shell=shell, language_info={"name": "python"})
            with contextlib.redirect_stdout(io.StringIO()), patch.object(IPython, "get_ipython", return_value=shell), patch("backprop.notebook_discovery._from_servers", return_value=None):
                shell.run_cell("value = 2", store_history=True)
                run = backprop.Run(project="real-ipython", dir=directory, _sink=backprop.MemorySink(), _deployment={},
                    settings={"save_code": True, "console": "off", "silent": True, "disable_git": True})
                shell.run_cell("value + 3", store_history=True)
                run.finish()
            history = json.loads((Path(run.dir) / "code/_session_history.ipynb").read_text())
            self.assertEqual([cell["source"] for cell in history["cells"]], ["value = 2", "value + 3"])
            self.assertNotIn(run._notebook._callback, shell.events.callbacks["post_run_cell"])
            shell.history_manager.end_session()


if __name__ == "__main__":
    unittest.main()
