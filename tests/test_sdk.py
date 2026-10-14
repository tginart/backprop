"""Behavioral contracts for the W&B subset, using no external dependencies.

Architecture: real public SDK calls -> durable journal -> MemorySink events.
Assertions concern observable rows, resume, file bytes, and upload failures.
Main callers: python -m unittest discover -s tests.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from importlib import import_module
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop as wandb
from backprop.transport import Journal, read_journal


class SDKTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sink = wandb.MemorySink()
        self.runs = []

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        wandb.run = None
        self.temp.cleanup()

    def new_run(self, **kwargs):
        run = wandb.Run(project="contract", dir=self.temp.name, _sink=self.sink,
                        _deployment={"app_url": "https://backprop.example"}, **kwargs)
        self.runs.append(run)
        return run

    def histories(self):
        return [event["payload"] for event in self.sink.events if event["type"] == "history"]

    def test_commit_and_explicit_step_semantics(self):
        run = self.new_run()
        run.log({"loss": 4}, commit=False)
        run.log({"accuracy": 0.2})
        run.log({"loss": 3}, step=7)
        run.log({"accuracy": 0.5}, step=7, commit=True)
        run.log({"loss": 2}, step=10)
        run.finish()
        self.assertEqual([row["step"] for row in self.histories()], [0, 7, 10])
        self.assertEqual(self.histories()[0]["metrics"], {"loss": 4, "accuracy": 0.2})
        self.assertEqual(self.histories()[1]["metrics"], {"loss": 3, "accuracy": 0.5})

    def test_past_step_warns_and_drops_like_upstream(self):
        run = self.new_run()
        run.log({"x": 1})
        with self.assertWarnsRegex(RuntimeWarning, "older"):
            run.log({"x": 2}, step=0)
        run.finish()
        self.assertEqual([row["metrics"] for row in self.histories()], [{"x": 1}])

    def test_custom_axes_hidden_and_summary(self):
        run = self.new_run()
        run.define_metric("epoch", hidden=True)
        run.define_metric("train/*", step_metric="epoch", summary="min,max,mean")
        run.log({"epoch": 4, "train/loss": 3})
        run.log({"train/loss": 1})
        expected = {"min": 1, "max": 3, "mean": 2}
        self.assertEqual(run.summary["train/loss"], expected)
        run.finish()
        self.assertEqual(self.histories()[1]["metrics"]["epoch"], 4)
        self.assertEqual(self.sink.events[-2]["payload"]["values"]["train/loss"], expected)

    def test_config_attribute_access_and_change_guard(self):
        run = self.new_run(config={"lr": 0.1})
        self.assertEqual(run.config.lr, 0.1)
        with self.assertRaisesRegex(ValueError, "allow_val_change"):
            run.config.update({"lr": 0.2})
        run.config.update({"lr": 0.2}, allow_val_change=True)
        run.summary.best = 0.8
        self.assertEqual(run.summary.best, 0.8)
        run.finish()
        self.assertEqual(run.config["lr"], 0.2)
        self.assertEqual(self.sink.events[-2]["payload"]["values"]["best"], 0.8)

    def test_finished_summary_warns_and_is_empty_without_losing_persisted_values(self):
        run = self.new_run()
        run.log({"loss": 2})
        run.finish()
        with self.assertWarnsRegex(RuntimeWarning, "Summary data not available in finished run"):
            self.assertEqual(run.summary.as_dict(), {})
        self.assertEqual(self.sink.events[-2]["type"], "summary")
        self.assertTrue(self.sink.events[-2]["payload"]["automatic"])
        self.assertEqual(self.sink.events[-2]["payload"]["values"]["loss"], 2)

    def test_incremental_and_mutable_tables(self):
        run = self.new_run()
        table = wandb.Table(columns=["x", "label"], log_mode="INCREMENTAL")
        table.add_data(1, "first")
        run.log({"samples": table})
        table.add_data(2, "second")
        run.log({"samples": table})
        mutable = wandb.Table(columns=["x"], data=[[1]], log_mode="MUTABLE")
        run.log({"mutable": mutable})
        mutable.add_data(2)
        run.log({"mutable": mutable})
        run.finish()
        first, second = [row["metrics"]["samples"] for row in self.histories()[:2]]
        self.assertEqual(first["data"], [[1, "first"]])
        self.assertEqual(second["data"], [[2, "second"]])
        self.assertEqual(second["row_offset"], 1)
        self.assertEqual(first["table_id"], second["table_id"])
        self.assertEqual(self.histories()[-1]["metrics"]["mutable"]["data"], [[1], [2]])

    def test_failed_serialization_does_not_advance_table(self):
        run = self.new_run()
        table = wandb.Table(columns=["x"], data=[[1]], log_mode="INCREMENTAL")
        with self.assertRaises(TypeError):
            run.log({"table": table, "bad": object()})
        self.assertEqual(table._offset, 0)
        run.log({"bad": "repaired"})
        run.log({"table": table})
        run.finish()
        self.assertEqual(self.histories()[0]["metrics"], {"bad": "repaired"})
        self.assertEqual(self.histories()[1]["metrics"]["table"]["data"], [[1]])

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Automatic histograms require optional NumPy")
    def test_histogram_edges_and_counts(self):
        histogram = wandb.Histogram([0, 0.5, 1], num_bins=2)
        self.assertEqual(histogram.histogram, [1, 2])
        self.assertEqual(histogram.bins, [0, 0.5, 1])

    def test_precomputed_histogram_preserves_counts_without_numpy(self):
        fixed = wandb.Histogram(np_histogram=([2, 3], [0, 1, 2]))
        self.assertEqual(fixed.histogram, [2, 3])
        # Pinned W&B validates lengths, but accepts caller-supplied edge order.
        self.assertEqual(wandb.Histogram(np_histogram=([1], [1, 0])).bins, [1, 0])

    def test_plot_series_preserves_names_and_xy(self):
        run = self.new_run()
        chart = wandb.plot.line_series([0, 1], [[2, 3], [4, 5]], keys=["a", "b"], xname="epoch")
        run.log({"comparison": chart})
        run.finish()
        value = self.histories()[0]["metrics"]["comparison"]
        self.assertEqual(value["fields"], {"x": "step", "y": "lineVal", "series": "lineKey"})
        self.assertEqual(value["string_fields"]["xname"], "epoch")
        self.assertEqual(value["table"]["data"], [[0, "a", 2], [1, "a", 3], [0, "b", 4], [1, "b", 5]])

    def test_resume_retains_config_step_and_summary_aggregation(self):
        first = self.new_run(id="persistent", config={"lr": 0.1})
        first.define_metric("loss", summary="mean")
        first.log({"loss": 3})
        first.finish()
        second = self.new_run(id="persistent", resume="must")
        self.assertTrue(second.resumed)
        self.assertEqual(second.step, 1)
        second.log({"loss": 1})
        self.assertEqual(second.summary["loss"], {"mean": 2})
        second.finish()
        self.assertEqual(self.sink.events[-2]["payload"]["values"]["loss"], {"mean": 2})
        self.assertEqual(second.config.lr, 0.1)
        self.assertEqual([row["step"] for row in self.histories()], [0, 1])

    def test_resume_retains_metadata_and_orders_local_remote_events(self):
        first = self.new_run(id="metadata", name="original", group="cohort", tags=["a"], notes="keep")
        first.config.update({"revision": 1})
        first.finish()
        # A later process changed config remotely while this process's journal
        # still contains its older copies. Canonical replay must choose latest.
        event = dict(self.sink.events[-1], event_id="remote-later", timestamp="2099-01-01T00:00:00+00:00",
                     type="config", payload={"values": {"revision": 2}})
        self.sink.events.append(event)
        resumed = self.new_run(id="metadata", resume="must")
        self.assertEqual(resumed.name, "original")
        self.assertEqual(resumed.tags, ("a",))
        self.assertEqual(resumed.group, "cohort")
        self.assertEqual(resumed.notes, "keep")
        self.assertEqual(resumed.config.revision, 2)

    def test_new_random_run_does_not_scan_remote_history(self):
        with patch.object(self.sink, "read_events", side_effect=AssertionError("must not scan")):
            self.new_run().finish()

    def test_run_link_identifies_entity_and_project_without_ambiguity(self):
        run = self.new_run(entity="another-team", id="same-run-id")
        query = parse_qs(urlsplit(run.url).query)
        self.assertEqual(query["project"], ["another-team/contract"])
        self.assertEqual(query["run"], ["same-run-id"])
        with self.assertRaises(wandb.UsageError):
            self.new_run(id="..")
        with self.assertRaises(wandb.UsageError):
            self.new_run(entity="ambiguous/team")

    def test_background_flush_recovers_after_explicit_retry(self):
        run = self.new_run(settings=wandb.Settings(flush_interval=0.01))
        run.log({"loss": 1})
        with patch.object(self.sink, "publish", side_effect=ConnectionError("temporary")):
            with patch.object(import_module("backprop.run").warnings, "warn"):
                run._thread.join(timeout=1)
        self.assertIsNotNone(run._error)
        run.flush()
        self.assertTrue(run._thread.is_alive())
        run.log({"loss": 2})
        deadline = time.monotonic() + 1
        while len(self.histories()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.histories()), 2)

    def test_large_table_is_externalized_without_losing_cells(self):
        run = self.new_run()
        content = "context " * 20_000
        run.log({"samples": wandb.Table(columns=["full_prompt"], data=[[content]])})
        run.finish()
        reference = self.histories()[0]["metrics"]["samples"]
        self.assertTrue(reference["external"])
        self.assertNotIn("data", reference)
        snapshot = json.loads(self.sink.files[reference["path"]])
        self.assertEqual(snapshot["data"], [[content]])

    def test_resume_must_and_never(self):
        with self.assertRaisesRegex(wandb.BackpropError, "missing"):
            self.new_run(id="missing", resume="must")
        run = self.new_run(id="existing")
        run.finish()
        with self.assertRaisesRegex(wandb.BackpropError, "exists"):
            self.new_run(id="existing", resume="never")

    def test_artifact_file_is_snapshotted_before_upload(self):
        source = Path(self.temp.name) / "messages.json"
        source.write_text('{"content":"original"}')
        run = self.new_run()
        artifact = wandb.Artifact("context", type="synthetic-project-policy-context")
        artifact.add_file(source, name="policy-messages.json")
        run.log_artifact(artifact)
        source.write_text("changed")
        run.finish()
        payload = next(event["payload"] for event in self.sink.events if event["type"] == "artifact")
        self.assertEqual(payload["files"][0]["name"], "policy-messages.json")
        self.assertEqual(self.sink.files[payload["files"][0]["path"]], b'{"content":"original"}')
        self.assertNotIn("_backprop_local_file", payload["files"][0])

    def test_offline_sync_and_media(self):
        run = wandb.Run(project="offline", mode="offline", dir=self.temp.name, _deployment={})
        run.log({"report": wandb.Html("<h1>Hello</h1>", inject=False)})
        run.finish()
        self.assertEqual(len(self.sink.events), 0)
        count = wandb.sync(run.sync_dir, sink=self.sink)
        self.assertGreater(count, 0)
        self.assertIn(b"<h1>Hello</h1>", self.sink.files.values())
        wandb.sync(run.sync_dir, sink=self.sink)
        self.assertEqual(len(self.sink.events), count)

    def test_explicit_media_mime_survives_filename_inference(self):
        run = self.new_run()
        original = self.sink.upload_file
        def inferred_upload(path, name):
            return {**original(path, name), "mime_type": "application/octet-stream"}
        with patch.object(self.sink, "upload_file", side_effect=inferred_upload):
            run.log({"report": wandb.Html("<h1>Hello</h1>"),
                     "image": wandb.Image(b"\x89PNG\r\n\x1a\n", mime_type="image/png")})
            run.finish()
        metrics = self.histories()[0]["metrics"]
        self.assertEqual(metrics["report"]["mime_type"], "text/html")
        self.assertEqual(metrics["image"]["mime_type"], "image/png")

    def test_failed_upload_keeps_queue_for_retry(self):
        run = self.new_run()
        run.log({"loss": 1})
        with patch.object(self.sink, "publish", side_effect=ConnectionError("unavailable")):
            with self.assertRaisesRegex(wandb.BackpropError, "remain"):
                run.flush()
        self.assertTrue((Path(run.sync_dir) / "events.jsonl").exists())
        run.flush()
        run.finish()
        self.assertEqual(len(self.histories()), 1)

    def test_disabled_creates_no_directory_or_network_calls(self):
        directory = Path(self.temp.name) / "disabled"
        with wandb.Run(project="disabled", mode="disabled", dir=directory, _deployment={}) as run:
            run.log({"loss": 2})
            self.assertEqual(run.summary.loss, 2)
        self.assertFalse(directory.exists())

    def test_context_failure_marks_failed(self):
        run = self.new_run()
        with self.assertRaises(ValueError):
            with run:
                raise ValueError("training failed")
        self.assertEqual(self.sink.events[-1]["payload"]["state"], "failed")

    def test_module_reinit_and_shim(self):
        # Active-run reinit policies apply to ordinary runs. Disabled init
        # always creates a fresh global handle, verified by the pinned probe.
        options = dict(mode="offline", dir=self.temp.name, _deployment={},
                       settings={"console": "off", "silent": True, "disable_git": True})
        first = wandb.init(project="a", **options)
        self.runs.append(first)
        self.assertIs(wandb.init(project="ignored", **options), first)
        second = wandb.init(project="b", reinit="finish_previous", **options)
        self.runs.append(second)
        self.assertTrue(first._closed)
        self.assertIs(wandb.run, second)
        third = wandb.init(project="c", reinit="create_new", **options)
        self.runs.append(third)
        self.assertIs(wandb.run, second)
        third.finish()
        wandb.finish()
        import wandb as shim
        self.assertIs(shim, wandb)

    def test_scoped_launcher_runs_unchanged_wandb_import(self):
        script = Path(self.temp.name) / "training.py"
        script.write_text("import os, wandb\n"
                          "assert wandb.__name__ == 'backprop'\n"
                          "assert os.environ['WANDB_API_KEY'] == 'backprop-foundry-readiness-sentinel'\n"
                          "with wandb.init(project='launcher', mode='disabled') as run:\n"
                          "    run.log({'loss': 0.5})\n"
                          "print('launcher verified')\n")
        sdk = str(Path(__file__).resolve().parents[1] / "sdk")
        result = subprocess.run([sys.executable, "-m", "backprop", "run", "--high-command", "--", str(script)],
                                env={**os.environ, "PYTHONPATH": sdk}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "launcher verified")

    def test_uncaught_training_failure_is_not_reported_as_finished(self):
        sdk = str(Path(__file__).resolve().parents[1] / "sdk")
        for suffix, ending, launcher in [("exception", "raise RuntimeError('training failed')", False),
                                         ("systemexit", "raise SystemExit(7)", True)]:
            directory = Path(self.temp.name) / suffix
            script = Path(self.temp.name) / f"{suffix}.py"
            script.write_text("import backprop as wandb\n"
                              f"run = wandb.init(project='failure', mode='offline', dir={str(directory)!r})\n"
                              "run.log({'loss': 1})\n" + ending + "\n")
            command = [sys.executable, "-m", "backprop", "run", "--", str(script)] if launcher else [sys.executable, str(script)]
            result = subprocess.run(command, env={**os.environ, "PYTHONPATH": sdk}, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            journal = next(directory.glob("*/*/events.jsonl"))
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertEqual(events[-1]["type"], "run_finish")
            self.assertEqual(events[-1]["payload"]["state"], "failed")
            self.assertEqual(events[-1]["payload"]["exit_code"], 7 if launcher else 1)


if __name__ == "__main__":
    unittest.main()
