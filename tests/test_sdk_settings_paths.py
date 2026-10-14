"""Workspace discovery and real diagnostic-file lifecycle contracts.

Architecture: temporary workspace -> Settings/Run -> diagnostic writes and links
-> finish. Assertions inspect file content and link targets, not just computed
strings. Direct helper cases cover unsupported links and independent run closure.
"""
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from importlib import import_module
from unittest.mock import patch

import backprop
from backprop.debug_logs import DebugLogs
from backprop.settings_paths import workspace_dir


class SettingsPathContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def helper(self, identity, *, symlink=True, token=None):
        settings = backprop.Settings(root_dir=self.root, run_id=identity, symlink=symlink)
        settings._sync_dir = str(Path(settings.wandb_dir) / "project" / identity)
        run = SimpleNamespace(settings=settings, id=identity, _sink=SimpleNamespace(token=token))
        logs = DebugLogs(run)
        self.addCleanup(logs.close)
        logs.start()
        return settings, logs

    def test_workspace_discovery_explicit_overrides_and_no_write(self):
        self.assertEqual(workspace_dir(self.root), self.root / "wandb")
        self.assertEqual(workspace_dir(self.root, True), self.root / ".wandb")
        self.assertEqual(list(self.root.iterdir()), [])
        (self.root / ".wandb").mkdir()
        self.assertEqual(workspace_dir(self.root), self.root / ".wandb")
        self.assertEqual(workspace_dir(self.root, False), self.root / "wandb")
        for override, expected in [(None, ".wandb"), (True, ".wandb"), (False, "wandb")]:
            settings = backprop.Settings(root_dir=self.root, use_dot_wandb=override)
            self.assertEqual(Path(settings.wandb_dir), self.root / expected)
            self.assertEqual(Path(settings.settings_workspace), self.root / expected / "settings")

    def test_settings_file_discovery_uses_the_selected_workspace(self):
        for directory, project in [(".wandb", "hidden-project"), ("wandb", "visible-project")]:
            (self.root / directory).mkdir()
            (self.root / directory / "settings").write_text("[default]\nproject=" + project + "\n")
        discovered = backprop.Settings(root_dir=self.root)
        discovered.update_from_system_settings()
        self.assertEqual(discovered.project, "hidden-project")
        explicit = backprop.Settings(root_dir=self.root, use_dot_wandb=False)
        explicit.update_from_system_settings()
        self.assertEqual(explicit.project, "visible-project")

    def test_logs_are_real_isolated_files_and_links_select_the_newest_run(self):
        root_handlers = list(logging.getLogger().handlers)
        first, first_logs = self.helper("first")
        first_logs.user("first user record")
        first_logs.internal("first transport record")
        second, second_logs = self.helper("second")
        second_logs.user("second user record")
        for link, target in [(second.sync_symlink_latest, second.sync_dir),
                             (second.log_symlink_user, second.log_user),
                             (second.log_symlink_internal, second.log_internal)]:
            self.assertTrue(Path(link).is_symlink())
            self.assertFalse(Path(os.readlink(link)).is_absolute())
            self.assertEqual(Path(link).resolve(), Path(target).resolve())
        first_logs.close()
        first_logs.user("ignored after closure")
        second_logs.internal("still open")
        self.assertIn("first user record", Path(first.log_user).read_text())
        self.assertNotIn("second user record", Path(first.log_user).read_text())
        self.assertNotIn("ignored after closure", Path(first.log_user).read_text())
        self.assertIn("first transport record", Path(first.log_internal).read_text())
        self.assertIn("still open", Path(second.log_internal).read_text())
        self.assertEqual(root_handlers, logging.getLogger().handlers)

    def test_disabling_links_keeps_real_files_and_failures_warn(self):
        settings, logs = self.helper("no-links", symlink=False)
        self.assertTrue(Path(settings.log_user).is_file())
        self.assertTrue(Path(settings.log_internal).is_file())
        self.assertFalse(Path(settings.log_symlink_user).exists())
        self.assertFalse(Path(settings.sync_symlink_latest).exists())
        with patch.object(Path, "symlink_to", side_effect=OSError("not supported")):
            with self.assertWarnsRegex(RuntimeWarning, "Could not create diagnostic link"):
                failed_settings, failed_logs = self.helper("unsupported")
        failed_logs.user("file usable after link failure")
        self.assertIn("file usable", Path(failed_settings.log_user).read_text())

    def test_diagnostics_redact_known_credentials_and_authorization_headers(self):
        with patch.dict(os.environ, {"BACKPROP_TOKEN": "environment-secret"}):
            settings, logs = self.helper("redacted", token="store-secret")
        logs.user("Failure using store-secret and environment-secret")
        logs.internal("Authorization: Bearer unrecognized-header-token")
        text = Path(settings.log_user).read_text() + Path(settings.log_internal).read_text()
        for secret in ["store-secret", "environment-secret", "unrecognized-header-token"]:
            self.assertNotIn(secret, text)
        self.assertIn("[redacted]", text)

    def test_run_paths_create_diagnostics_and_respect_dot_directory(self):
        (self.root / ".wandb").mkdir()
        run = backprop.Run(project="paths", dir=self.root, _sink=backprop.MemorySink(),
                           _deployment={}, settings={"console": "off", "silent": True})
        self.addCleanup(run.finish)
        self.assertEqual(Path(run.settings.wandb_dir).resolve(), (self.root / ".wandb").resolve())
        run.log({"loss": 1})
        run.flush()
        run.finish()
        self.assertTrue(Path(run.settings.log_user).is_file())
        self.assertTrue(Path(run.settings.log_internal).is_file())
        self.assertIn("history", Path(run.settings.log_internal).read_text())
        self.assertTrue(all(not logger.handlers for logger in run._debug._loggers.values()))
        self.assertEqual(Path(run.settings.sync_symlink_latest).resolve(), Path(run.sync_dir))

    def test_disabled_run_does_not_create_debug_files(self):
        run = backprop.Run(project="disabled-paths", dir=self.root, mode="disabled", settings={"console": "off", "silent": True})
        run.finish()
        self.assertFalse(Path(run.settings.log_user).exists())
        self.assertFalse(Path(run.settings.sync_symlink_latest).exists())

    def test_failed_initialization_closes_created_debug_handlers(self):
        created = []
        def create(run):
            logs = DebugLogs(run)
            created.append(logs)
            return logs
        class FailedSink(backprop.MemorySink):
            def publish(self, events):
                raise OSError("Deliberate initialization upload failure")
        with patch.object(import_module("backprop.run"), "DebugLogs", side_effect=create):
            with self.assertRaises(backprop.BackpropError):
                backprop.Run(project="failed-paths", dir=self.root, _sink=FailedSink(),
                             _deployment={}, settings={"console": "off", "silent": True})
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0]._closed)
        self.assertTrue(all(not logger.handlers for logger in created[0]._loggers.values()))


if __name__ == "__main__":
    unittest.main()
