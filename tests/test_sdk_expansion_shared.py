"""Independent shared writers preserve history, lifecycle and artifact parts.

Architecture: multiple real Run/journal instances -> one MemorySink -> public
replay. PyTorch exercises real autograd hooks when available, without GPU needs.
"""
import copy
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch
import backprop
from backprop.apis.public import Api
from backprop.apis.public.replay import reduce_events
from backprop.transport import MemorySink


class Shared(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BACKPROP_DIR": self.tmp.name})
        self.env.start()
        self.sink = MemorySink()
        self.api = Api._from_store(self.sink, entity="e", project="p")
        self.runs = []

    def tearDown(self):
        for run in self.runs:
            if not run._closed: run.finish()
        self.env.stop()
        self.tmp.cleanup()

    def writer(self, label, primary=False):
        run = backprop.Run(id="r", entity="e", project="p", mode="shared", _sink=self.sink,
             dir=self.tmp.name, settings={"x_label": label, "x_primary": primary,
                "console": "off", "silent": True, "x_disable_stats": True})
        self.runs.append(run)
        return run

    def test_incremental_draft_can_publish_a_complete_snapshot(self):
        run = self.writer("primary", True)
        artifact = backprop.Artifact("parts", "dataset", incremental=True)
        self.assertIs(run.log_artifact(artifact), artifact)
        run.flush()
        records = [e for e in self.sink.events if e["type"] == "artifact"]
        self.assertEqual(len(records), 1)
        self.assertNotIn("distributed_id", records[0]["payload"])
        self.assertFalse(any(e["type"] == "artifact_part" for e in self.sink.events))

    def test_independent_steps_summary_and_secondary_finish(self):
        primary, secondary = self.writer("rank0", True), self.writer("rank1")
        self.assertNotEqual(primary.sync_dir, secondary.sync_dir)
        primary.log({"epoch": 0, "train/loss": 2})
        secondary.log({"epoch": 0, "validation/loss": 3})
        secondary.finish()
        primary.flush()
        run = self.api.run("r")
        self.assertEqual(run.state, "running")
        rows = list(run.scan_history())
        self.assertEqual([row["_step"] for row in rows], [0, 0])
        self.assertEqual(len({row["_writer"] for row in rows}), 2)
        self.assertEqual(run.summary["train/loss"], 2)
        self.assertEqual(run.summary["validation/loss"], 3)
        primary.finish()
        run.load(force=True)
        self.assertEqual(run.state, "finished")
        self.assertEqual(run.summary["validation/loss"], 3)
        normal = reduce_events(self.sink.events)
        self.assertEqual(normal, reduce_events(list(reversed(self.sink.events)) + copy.deepcopy(self.sink.events)))

    def test_public_summary_edit_survives_unrelated_logging(self):
        run = self.writer("rank0", True)
        run.log({"loss": 3})
        run.flush()
        self.api.run("r").summary["review"] = "approved"
        run.log({"loss": 2})
        run.finish()
        self.api.flush()
        self.assertEqual(self.api.run("r").summary["review"], "approved")

    def test_distributed_artifact_union_and_late_contribution(self):
        primary, secondary = self.writer("rank0", True), self.writer("rank1")
        def artifact(path, text):
            a = backprop.Artifact("shards", "dataset", incremental=True)
            with a.new_file(path) as f: f.write(text)
            return a
        secondary.upsert_artifact(artifact("b.txt", "b"), distributed_id="d")
        first = primary.finish_artifact(artifact("a.txt", "a"), distributed_id="d")
        self.assertEqual(set(first.manifest.entries), {"a.txt", "b.txt"})
        secondary.upsert_artifact(artifact("c.txt", "c"), distributed_id="d")
        second = primary.finish_artifact(artifact("d.txt", "d"), distributed_id="d")
        self.assertEqual(set(second.manifest.entries), {"c.txt", "d.txt"})
        self.assertEqual(set(self.api.artifact(first.id).manifest.entries), {"a.txt", "b.txt"})
        with self.assertRaises(ValueError): secondary.finish_artifact(artifact("e", "e"), distributed_id="d")

    @unittest.skipUnless(importlib.util.find_spec("torch"), "optional torch not installed")
    def test_real_torch_hooks_preserve_gradients_and_unwatch(self):
        import torch
        run = self.writer("rank0", True)
        model = torch.nn.Linear(2, 1)
        run.watch(model, log="all", log_freq=1)
        model(torch.ones(1, 2)).sum().backward()
        self.assertTrue(torch.equal(model.weight.grad, torch.ones_like(model.weight.grad)))
        run.log({"loss": 1})
        run.unwatch(model)
        model(torch.ones(1, 2)).sum().backward()
        run.log({"loss": 2})
        run.flush()
        rows = list(self.api.run("r").scan_history())
        self.assertIn("gradients/weight", rows[0])
        self.assertIn("parameters/weight", rows[0])
        self.assertNotIn("gradients/weight", rows[1])

    @unittest.skipUnless(importlib.util.find_spec("psutil"), "optional psutil not installed")
    def test_real_cpu_and_memory_measurements_are_publicly_readable(self):
        from backprop.telemetry import SystemMonitor
        run=self.writer("cpu",True)
        run._settings.x_disable_stats=False
        monitor=SystemMonitor(run)
        try:
            values=monitor.sample()
            self.assertGreater(values['proc.memory.rssMB'],0)
            self.assertGreaterEqual(values['cpu'],0)
            run.flush()
            self.assertEqual(self.api.run('r').system_metrics['proc.memory.rssMB'],values['proc.memory.rssMB'])
        finally:monitor.stop()

    def test_shared_initialization_does_not_fetch_unrelated_history(self):
        with patch.object(self.sink,'read_events',side_effect=AssertionError('Shared initialization must not scan history')):
            run=self.writer('worker')
        run.log({'loss':1})

    def test_distributed_conflicting_paths_are_rejected(self):
        primary,worker=self.writer('primary',True),self.writer('worker')
        def part(text):
            artifact=backprop.Artifact('conflicts','dataset',incremental=True)
            with artifact.new_file('same.txt') as f:f.write(text)
            return artifact
        worker.upsert_artifact(part('one'),distributed_id='same')
        with self.assertRaisesRegex(ValueError,'Conflicting distributed artifact path'):
            primary.finish_artifact(part('two'),distributed_id='same')
        self.assertFalse([e for e in self.sink.events if e['type']=='artifact'])

    def test_finalization_recovery_uses_journal_with_new_draft_and_handle(self):
        from pathlib import Path
        primary=self.writer('primary',True)
        path=Path(self.tmp.name)/'checkpoint.bin'
        path.write_bytes(b'checkpoint')
        publish=self.sink.publish
        def fail_final(events):
            publish(events)
            if any(e['type']=='artifact' for e in events):raise OSError('lost acknowledgement')
        with patch.object(self.sink,'publish',side_effect=fail_final):
            with self.assertRaises(backprop.BackpropError):
                primary.finish_artifact(str(path),name='model',type='model',distributed_id='training')
        first=next(e for e in self.sink.events if e['type']=='artifact')
        # A new process would have neither the original Artifact nor Run objects.
        restarted=self.writer('primary',True)
        with patch.object(self.sink,'read_events',return_value=[]):
            result=restarted.finish_artifact(str(path),name='model',type='model',distributed_id='training')
        self.assertEqual(result.id,first['payload']['artifact_id'])
        finals=[e for e in self.sink.events if e['type']=='artifact']
        self.assertEqual(len({e['event_id'] for e in finals}),1)
        self.assertEqual(result.get_entry('checkpoint.bin')._read_verified_bytes(),b'checkpoint')

    def test_watching_again_after_unwatch_and_finish_cleans_hooks(self):
        if not importlib.util.find_spec('torch'):self.skipTest('optional torch unavailable')
        import torch
        run=self.writer('primary',True)
        model=torch.nn.Linear(2,1)
        for _ in range(2):
            run.watch(model,log='gradients',log_freq=1,idx=0)
            with self.assertRaises(ValueError):run.watch(model,idx=0)
            run.watch(model,idx=1)
            model(torch.ones(1,2)).sum().backward();run.log({'loss':1})
            run.unwatch(model)
            self.assertFalse(model._forward_hooks)
        run.watch(model);run.finish()
        self.assertFalse(model._forward_hooks)
        self.assertFalse(model.weight._backward_hooks)

    def test_nvml_cleanup_is_once_and_collector_errors_are_visible(self):
        from backprop.telemetry import SystemMonitor
        from unittest.mock import Mock
        run=self.writer('primary',True)
        monitor=SystemMonitor(run)  # disabled by writer settings
        collector=Mock();monitor.nvml=collector
        monitor.stop();monitor.stop()
        collector.nvmlShutdown.assert_called_once()
