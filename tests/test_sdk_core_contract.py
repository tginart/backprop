"""Core contracts derived from the pinned W&B 0.30 source, without network I/O.

Architecture: public calls -> real Run/journal -> deterministic MemorySink.
Tests emphasize observable behavior, rejected-input atomicity and lifecycle.
Main callers: python -m unittest discover -s tests -p 'test_sdk_core*.py'.
"""
from collections import UserDict
import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop as wandb


class CoreContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sink = wandb.MemorySink()
        self.runs = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        wandb.teardown()

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        wandb.teardown()
        self.environment.stop()
        self.temp.cleanup()

    def new(self, **kwargs):
        run = wandb.Run(project="core", dir=self.temp.name, _sink=self.sink,
                        _deployment={"app_url": "https://example.invalid"}, **kwargs)
        self.runs.append(run)
        return run

    def events(self, kind):
        return [event["payload"] for event in self.sink.events if event["type"] == kind]

    def test_metadata_properties_publish_and_identity_is_readonly(self):
        run = self.new()
        run.name = "renamed"
        run.notes = "new notes"
        run.tags = ["a"]
        run.tags += ("b",)
        run.flush()
        self.assertEqual(run.tags, ("a", "b"))
        self.assertEqual(self.events("run_metadata")[-1], {"values": {"tags": ["a", "b"]}})
        self.assertEqual(len(self.events("run_start")), 1)
        with self.assertRaises(AttributeError):
            run.id = "other"
        with self.assertRaises(wandb.Error):
            run.typo = 1

    def test_metadata_updates_survive_resume(self):
        run = self.new(id="resumed")
        run.name = "later"
        run.tags = ["updated"]
        run.finish()
        other = self.new(id="resumed", resume="must")
        self.assertEqual(other.name, "later")
        self.assertEqual(other.tags, ("updated",))

    def test_nested_summary_publishes_updates_and_deletions_immediately(self):
        run = self.new()
        run.summary.metrics = {"validation": {"score": 0.5, "remove": 9}}
        run.summary.metrics.validation.score = 0.75
        del run.summary.metrics.validation.remove
        run.flush()
        self.assertEqual(self.events("summary")[-1]["values"], {"metrics": {"validation": {"score": 0.75}}})
        del run.summary.metrics
        run.flush()
        self.assertEqual(self.events("summary")[-1]["deleted"], ["metrics"])
        del run.summary.nonexistent

    def test_config_named_argument_namespace_and_key_normalization(self):
        run = self.new(config=argparse.Namespace(lr=0.1))
        run.config.update(d={"--epochs--": 4, "_internal": 9})
        self.assertEqual(run.config.epochs, 4)
        self.assertNotIn("_internal", run.config.keys())
        self.assertEqual(run.config.as_dict(), {"lr": 0.1, "epochs": 4})
        run.config.update(argparse.Namespace(lr=0.2), allow_val_change=True)
        self.assertEqual(run.config.lr, 0.2)
        with self.assertRaises(wandb.ConfigError):
            run.config.lr = 0.3

    def test_config_update_atomicity_locks_and_static_snapshot(self):
        run = self.new(config={"a": 1})
        with self.assertRaises(wandb.ConfigError):
            run.config.update({"new": 2, "a": 3})
        self.assertNotIn("new", run.config)
        run.config.update_locked({"lock": 2}, user="owner")
        with warnings.catch_warnings(record=True) as captured:
            run.config.update({"lock": 3})
        self.assertTrue(captured)
        self.assertEqual(run.config.lock, 2)
        run.config.setdefaults({"a": 5, "b": 6})
        snapshot = run.config_static
        run.config.update({"a": 7}, allow_val_change=True)
        self.assertEqual(snapshot.a, 1)
        with self.assertRaises(AttributeError):
            snapshot.a = 9

    def test_rejected_history_input_does_not_change_pending_or_table(self):
        run = self.new()
        table = wandb.Table(columns=["x"], data=[[1]], log_mode="INCREMENTAL")
        run.log({"valid": 1}, commit=False)
        with self.assertRaises(TypeError):
            run.log({"table": table, "bad": object()})
        self.assertEqual(table._offset, 0)
        self.assertEqual(run._pending, {"valid": 1})
        with self.assertRaises(TypeError):
            run.log({4: 2}, step=10)
        self.assertEqual(run.step, 0)
        run.log({"next": 2})
        run.finish()
        self.assertEqual(self.events("history")[0]["metrics"], {"valid": 1, "next": 2})

    def test_mapping_input_and_past_step_warn_drop(self):
        run = self.new()
        run.log(UserDict(loss=1))
        with warnings.catch_warnings(record=True) as captured:
            run.log({"loss": 9}, step=0)
        self.assertTrue(captured)
        self.assertEqual(run.summary.loss, 1)
        run.finish()
        self.assertEqual(len(self.events("history")), 1)

    def test_metric_return_defaults_types_and_validation(self):
        run = self.new()
        metric = run.define_metric("epoch")
        self.assertIsNone(metric.step_metric)
        self.assertFalse(metric.step_sync)
        self.assertIsNone(metric.hidden)
        self.assertIsNone(metric.summary)
        loss = run.define_metric("loss", step_metric=metric, summary="MIN,MAX")
        self.assertEqual(loss.summary, ("min", "max"))
        self.assertTrue(loss.step_sync)
        with self.assertRaises(AttributeError):
            loss.name = "other"
        with self.assertRaises(wandb.Error):
            run.define_metric("bad", hidden="yes")
        with self.assertRaises(wandb.Error):
            run.define_metric("bad", step_metric=argparse.Namespace(name="epoch"))

    def test_metric_copy_first_text_nested_and_live_summary_snapshot(self):
        run = self.new()
        run.define_metric("copy", summary="copy")
        run.define_metric("label", summary="first,last")
        run.define_metric("train.loss", summary="min,max,mean")
        run.log({"copy": 3, "label": "a", "train": {"loss": 4}})
        run.log({"copy": 2, "label": "b", "train": {"loss": 2}})
        run.flush()
        self.assertEqual(run.summary.copy, 2)
        self.assertEqual(run.summary.label, {"first": "a", "last": "b"})
        self.assertEqual(run.summary.train.loss, {"min": 2, "max": 4, "mean": 3})
        self.assertEqual(self.events("history")[-1]["summary"]["train"]["loss"]["mean"], 3)

    def test_explicit_summary_resets_reduction_mode(self):
        run = self.new()
        run.define_metric("loss", summary="min")
        run.log({"loss": 4})
        run.summary.loss = 2
        run.log({"loss": 3})
        self.assertEqual(run.summary.loss, 3)

    def test_create_new_participates_in_reinit_and_finish_clears_global(self):
        # Disabled handles bypass reinit in the pinned SDK. Exercise the real
        # active-run policy with offline journals and no remote credentials.
        options = {"mode": "offline", "dir": self.temp.name,
                   "settings": {"silent": True, "console": "off", "x_disable_stats": True}}
        a = wandb.init(project="a", **options)
        b = wandb.init(project="b", reinit="create_new", **options)
        self.assertIs(wandb.run, a)
        self.assertIs(wandb.init(reinit="return_previous", **options), b)
        with self.assertRaises(wandb.Error):
            wandb.init(resume="must", **options)
        c = wandb.init(project="c", reinit="finish_previous", **options)
        self.assertTrue(a._closed and b._closed)
        c.finish()
        self.assertIsNone(wandb.run)
        with self.assertRaises(wandb.Error):
            wandb.log({"x": 1})

    def test_unknown_init_options_raise_even_with_active_run(self):
        wandb.init(mode="disabled")
        with self.assertRaises(TypeError):
            wandb.init(typo=True)

    def test_auto_resume_recovers_interrupted_identifier(self):
        first = self.new(resume="auto")
        first.log({"loss": 1})
        first.flush()
        # Stop worker without finish, matching an interrupted process journal.
        first._stop.set()
        first._thread.join()
        second = self.new(resume="auto")
        self.assertEqual(first.id, second.id)
        self.assertTrue(second.resumed)
        self.assertEqual(second.starting_step, 1)
        second.finish()
        self.assertFalse(second._auto_resume_path.exists())

    def test_disabled_is_permissive_and_offline_urls_absent(self):
        run = self.new(mode="disabled")
        marker = object()
        run.log({"opaque": marker}, step=-2)
        self.assertIs(run.summary.opaque, marker)
        self.assertEqual(run.step, 0)
        self.assertIsNone(run.url)
        offline = self.new(mode="offline")
        self.assertIsNone(offline.url)
        self.assertIsNone(offline.project_url)

    def test_settings_precedence_validation_and_detached_copy(self):
        with patch.dict(os.environ, {"WANDB_PROJECT": "env-project", "WANDB_MODE": "offline"}):
            wandb.setup(settings={"project": "setup-project"})
            run = wandb.init(project="explicit", settings={"mode": "disabled"})
        self.assertEqual(run.project, "explicit")
        self.assertTrue(run.disabled)
        settings = run.settings
        settings.mode = "online"
        self.assertTrue(run.disabled)
        with self.assertRaises(wandb.UsageError):
            wandb.Settings(flush_interval=0)
        with self.assertRaises(wandb.UsageError):
            wandb.Settings(typo=True)

    def test_run_file_live_end_and_safe_restore(self):
        source = Path(self.temp.name) / "source.txt"
        source.write_text("first")
        run = self.new()
        names = run.save(source, base_path=self.temp.name, policy="live")
        self.assertEqual(Path(names[0]).parent, Path(run.dir))
        source.write_text("second")
        run.flush()
        self.assertEqual(len([row for row in self.events("file") if row["name"] == "source.txt"]), 2)
        restore_root = Path(self.temp.name) / "restored"
        with run.restore("source.txt", root=restore_root) as restored:
            self.assertEqual(restored.read(), "second")
        with self.assertRaises(wandb.UsageError):
            run.restore("../escape")
        other = Path(self.temp.name) / "end.txt"
        other.write_text("before")
        run.save(other, base_path=self.temp.name, policy="end")
        other.write_text("after")
        run.finish()
        reference = next(p["file"] for p in self.events("file") if p["name"] == "end.txt")
        self.assertEqual(self.sink.download_file(reference), b"after")

    def test_write_logs_normalizes_newline_and_ignores_finished_calls(self):
        run = self.new(settings={"x_disable_stats": True})
        run.write_logs("hello")
        run.finish()
        before = len(self.sink.events)
        run.write_logs("ignored")
        self.assertEqual(len(self.sink.events), before)
        self.assertEqual(self.events("console"), [{"text": "hello\n", "stream": "stdout"}])

    def test_status_reports_pending_journal_and_full_durability(self):
        run = self.new()
        run.log({"x": 1})
        status = run.status()
        self.assertGreater(status.sync_items_pending, 0)
        run.flush()
        self.assertEqual(run.status().sync_items_pending, 0)
        self.assertIsNotNone(run.status().sync_time)


if __name__ == "__main__":
    unittest.main()
