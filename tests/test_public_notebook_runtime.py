"""Public notebook display against an existing genuine IPython runtime.

Architecture: isolated InteractiveShell with in-memory history -> real display
publisher capture -> Run/Project iframe output and File's deliberate no-display
contract. No network, installed extensions, user profile, or notebook server is
used. Main callers: optional runtime verification and unittest discovery.
"""
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop


@unittest.skipUnless(importlib.util.find_spec("IPython"), "Optional IPython runtime is unavailable")
class RealPublicNotebookDisplay(unittest.TestCase):
    def setUp(self):
        from IPython.core.interactiveshell import InteractiveShell
        from traitlets.config import Config
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Config()
        config.HistoryManager.hist_file = ":memory:"
        config.IPKernelApp.connection_file = "jupyter-test-connection.json"
        self.shell = InteractiveShell.instance(config=config, ipython_dir=self.temp.name)
        self.shell.kernel = SimpleNamespace(shell=self.shell, language_info={"name": "python"})
        self.addCleanup(self.shell.history_manager.end_session)
        self.addCleanup(InteractiveShell.clear_instance)
        self.store = backprop.MemorySink()
        self.writer = backprop.Run(project="notebook-display", entity="test", id="display", dir=self.temp.name,
            settings={"console": "off", "silent": True, "disable_git": True, "save_code": False, "flush_interval": 3600},
            _sink=self.store, _deployment={"app_url": "https://example.invalid/backprop"})
        self.addCleanup(self.writer.finish)
        source = Path(self.temp.name) / "file.txt"
        source.write_text("display fixture")
        self.writer.save(source, base_path=self.temp.name, policy="now")
        self.writer.flush()
        self.api = backprop.Api._from_store(self.store, entity="test", project="notebook-display",
            config={"app_url": "https://example.invalid/backprop"})

    def test_public_run_and_project_emit_real_html_display(self):
        from IPython.utils.capture import capture_output
        for value in (self.api.run("display"), self.api.project("notebook-display", "test")):
            with self.subTest(type=type(value).__name__), capture_output() as captured:
                self.assertTrue(value.display(height=321, hidden=True))
            self.assertEqual(len(captured.outputs), 1)
            markup = captured.outputs[0].data["text/html"]
            self.assertIn("<iframe", markup)
            self.assertIn("height:321px", markup)
            self.assertIn("display:none", markup)
            self.assertIn("example.invalid/backprop", markup)

    def test_file_display_retains_no_inline_renderer_contract(self):
        from IPython.utils.capture import capture_output
        file = self.api.run("display").file("file.txt")
        with capture_output() as captured:
            self.assertIsNone(file.to_html())
            self.assertFalse(file.display())
        self.assertEqual(captured.outputs, [])

    def test_active_silent_run_suppresses_public_display(self):
        from IPython.utils.capture import capture_output
        with patch.object(backprop, "run", self.writer), capture_output() as captured:
            self.assertFalse(self.api.run("display").display())
            self.assertFalse(self.api.project("notebook-display", "test").display())
        self.assertEqual(captured.outputs, [])


if __name__ == "__main__":
    unittest.main()
