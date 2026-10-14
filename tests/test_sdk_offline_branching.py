"""Offline branch recovery against an isolated remote event store.

Architecture: keep the parent in a different directory -> log without network
access -> resolve through the normal sync path -> assert public-reader state.
Faults cover missing parents, ambiguous writes, frozen snapshots, partial
acknowledgements, concurrent sync and continued offline logging.
Main callers: unittest discovery; no credentials or installed W&B are needed.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop as wandb
from backprop.transport import BackpropError, read_journal, sync
from backprop.apis.public.replay import reduce_events


class OfflineBranchContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = wandb.MemorySink()
        self.runs = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        self.environment.stop()
        self.temp.cleanup()

    def new(self, id, *, offline=False, source=None, rewind=False, config=None, **kwargs):
        settings = {"console": "off", "silent": True, "disable_git": True}
        if source:
            settings["resume_from" if rewind else "fork_from"] = source
        run = wandb.Run(project="branches", entity="owner", id=id,
            dir=self.root / ("offline" if offline else "parents"),
            mode="offline" if offline else "online", settings=settings,
            config=config, _sink=None if offline else self.store, _deployment={}, **kwargs)
        self.runs.append(run)
        return run

    def parent(self, *, id="parent", count=3):
        run = self.new(id, name="parent-name", tags=["source"], config={"lr": 0.01, "inherited": True})
        run.define_metric("loss", summary="mean,min")
        run.define_metric("accuracy", step_metric="epoch")
        run.summary["manual"] = {"value": 7}
        for index in range(count):
            run.log({"loss": 8-4*index, "epoch": index, "accuracy": 0.5+index/10})
        run.finish()
        return run

    def state(self, run):
        return reduce_events(self.store.read_events(project=run.project, entity=run.entity, run_id=run.id))

    def test_offline_init_accepts_absent_parent_without_network_and_persists_intent(self):
        with patch.object(self.store, "read_events", side_effect=AssertionError("offline read")), patch.object(self.store, "publish", side_effect=AssertionError("offline write")):
            child = wandb.Run(project="branches", entity="owner", id="child", dir=self.root / "offline",
                mode="offline", settings={"console": "off", "silent": True, "fork_from": "missing?_step=3"},
                _sink=self.store, _deployment={})
            self.runs.append(child)
            self.assertEqual(child.starting_step, 4)
            self.assertEqual(child.step, 4)
            child.log({"loss": 1})
            child.finish()
        marker = json.loads((Path(child.sync_dir) / "branch-intent.json").read_text())
        self.assertEqual((marker["source_id"], marker["run_id"], marker["cutoff"]), ("missing", "child", 3))
        self.assertEqual(len(self.store.events), 0)
        with self.assertRaisesRegex(BackpropError, "sync the parent first"):
            sync(child.sync_dir, sink=self.store)
        self.assertFalse((Path(child.sync_dir) / "branch-parent.json").exists())
        self.assertEqual(len(self.store.events), 0)

    def test_remote_fork_merges_parent_metrics_config_summary_and_metadata(self):
        parent = self.parent()
        parent_before = json.dumps(self.store.events, sort_keys=True)
        child = self.new("child", offline=True, source="parent?_step=1", config={"lr": 0.02}, tags=[])
        child.define_metric("loss", summary="max")
        child.log({"loss": 2, "accuracy": 0.9})
        child.summary["manual"] = {"value": 10}
        child.config.update({"new": "child"})
        child.finish()
        originals = (Path(child.sync_dir) / "events.jsonl").read_bytes()
        count = sync(child.sync_dir, sink=self.store)
        self.assertEqual(count, child._journal.count)
        self.assertEqual((Path(child.sync_dir) / "events.jsonl").read_bytes(), originals)
        state = self.state(child)
        self.assertEqual([row["_step"] for row in state["history"]], [0, 1, 2])
        self.assertAlmostEqual(state["summary"]["loss"]["mean"], 14/3)
        self.assertEqual(state["summary"]["loss"]["min"], 2)
        self.assertEqual(state["summary"]["loss"]["max"], 8)
        self.assertEqual(state["summary"]["manual"], {"value": 10})
        self.assertEqual(state["history"][-1]["epoch"], 1)
        self.assertEqual(state["config"], {"lr": 0.02, "inherited": True, "new": "child"})
        self.assertEqual(state["metadata"]["name"], "parent-name")
        self.assertEqual(state["metadata"]["tags"], [])
        self.assertEqual(json.dumps(self.store.read_events(run_id=parent.id), sort_keys=True), parent_before)
        self.assertEqual(len(read_journal(child.sync_dir)), len(self.store.read_events(run_id=child.id)))
        source_times = [event["timestamp"] for event in self.store.read_events(run_id=parent.id)
                        if event["type"] == "history"][:2]
        copied_times = [event["timestamp"] for event in self.store.read_events(run_id=child.id)
                        if event["type"] == "history"][:2]
        self.assertEqual(source_times, copied_times)

    def test_remote_rewind_replaces_future_rows_and_keeps_aggregate_prefix(self):
        self.parent()
        rewind = self.new("parent", offline=True, source="parent?_step=1", rewind=True)
        rewind.log({"loss": 2})
        rewind.finish()
        sync(rewind.sync_dir, sink=self.store)
        state = self.state(rewind)
        self.assertEqual([row["loss"] for row in state["history"]], [8, 4, 2])
        self.assertAlmostEqual(state["summary"]["loss"]["mean"], 14/3)
        self.assertEqual(state["config"]["inherited"], True)
        ids = [event["event_id"] for event in self.store.events]
        sync(rewind.sync_dir, sink=self.store)
        self.assertEqual([event["event_id"] for event in self.store.events], ids)

    def test_missing_parent_can_be_created_then_same_branch_retried(self):
        child = self.new("child", offline=True, source="parent?_step=0")
        child.log({"loss": 2})
        child.finish()
        with self.assertRaises(BackpropError):
            sync(child.sync_dir, sink=self.store)
        self.parent()
        sync(child.sync_dir, sink=self.store)
        self.assertEqual([row["loss"] for row in self.state(child)["history"]], [8, 2])

    def test_ambiguous_publish_reuses_parent_snapshot_and_stable_ids(self):
        self.parent()
        child = self.new("child", offline=True, source="parent?_step=1")
        child.log({"loss": 2})
        publish = self.store.publish
        def ambiguous(events):
            publish(events)
            raise OSError("response lost after accepted publish")
        with patch.object(self.store, "publish", side_effect=ambiguous):
            with self.assertRaisesRegex(BackpropError, "response lost"):
                sync(child.sync_dir, sink=self.store)
        first = [event for event in self.store.events if event["run_id"] == child.id]
        snapshot = (Path(child.sync_dir) / "branch-parent.json").read_bytes()
        child.log({"loss": 1})
        child.finish()
        with patch.object(self.store, "read_events", side_effect=AssertionError("snapshot must be frozen")):
            sync(child.sync_dir, sink=self.store)
        self.assertEqual(snapshot, (Path(child.sync_dir) / "branch-parent.json").read_bytes())
        child_events = [event for event in self.store.events if event["run_id"] == child.id]
        self.assertEqual(child_events[len(first):len(first)*2], first)
        self.assertEqual([row["loss"] for row in self.state(child)["history"]], [8, 4, 2, 1])
        ack = json.loads((Path(child.sync_dir) / "ack.json").read_text())
        self.assertEqual(ack["count"], child._journal.count)

    def test_files_artifacts_and_rich_history_remain_readable_from_child(self):
        parent = self.new("parent")
        path = self.root / "weights.bin"
        path.write_bytes(b"immutable weights")
        artifact = wandb.Artifact("weights", type="model")
        artifact.add_file(path)
        parent.log_artifact(artifact)
        parent.config.update({"model": artifact})
        parent.save(path, base_path=self.root)
        parent.log({"table": wandb.Table(columns=["x"], data=[[1]])})
        parent.finish()
        child = self.new("child", offline=True, source="parent?_step=0")
        child.log({"loss": 2})
        child.finish()
        sync(child.sync_dir, sink=self.store)
        state = self.state(child)
        self.assertEqual(self.store.download_file(state["files"]["weights.bin"]), b"immutable weights")
        self.assertEqual(state["artifacts"][0]["artifact_id"], artifact.id)
        self.assertEqual(self.store.download_file(state["artifacts"][0]["files"][0]), b"immutable weights")
        self.assertEqual(state["config"]["model"]["artifact_id"], artifact.id)
        self.assertEqual(state["history"][0]["table"]["data"], [[1]])

    def test_partial_upload_maps_raw_ack_and_continued_logging_appends_only(self):
        self.parent(count=110)
        child = self.new("child", offline=True, source="parent?_step=109")
        child.log({"loss": 2})
        publish, calls = self.store.publish, []
        def fail_second(events):
            calls.append(events)
            if len(calls) == 2:
                raise OSError("temporary failure")
            publish(events)
        with patch.object(self.store, "publish", side_effect=fail_second):
            with self.assertRaises(BackpropError):
                sync(child.sync_dir, sink=self.store)
        self.assertEqual(json.loads((Path(child.sync_dir) / "ack.json").read_text())["count"], 0)
        self.assertEqual(json.loads((Path(child.sync_dir) / "branch-ack.json").read_text())["count"], 100)
        sync(child.sync_dir, sink=self.store)
        prepared_before = (Path(child.sync_dir) / "branch-events.jsonl").read_bytes()
        child.log({"loss": 1})
        child.finish()
        sync(child.sync_dir, sink=self.store)
        self.assertTrue((Path(child.sync_dir) / "branch-events.jsonl").read_bytes().startswith(prepared_before))
        events = self.store.read_events(run_id=child.id)
        self.assertEqual(len(events), len({event["event_id"] for event in events}))
        self.assertEqual(len(self.state(child)["history"]), 112)

    def test_existing_destination_and_wrong_scope_publish_nothing(self):
        self.parent()
        self.new("child").finish()
        child = self.new("child", offline=True, source="parent?_step=0")
        child.finish()
        before = len(self.store.events)
        with self.assertRaisesRegex(BackpropError, "destination.*exists"):
            sync(child.sync_dir, sink=self.store)
        self.assertEqual(len(self.store.events), before)
        other = self.new("other", offline=True, source="parent?_step=0")
        other.finish()
        with patch.object(self.store, "read_events", return_value=[{**self.store.events[0], "project": "other-project"}]):
            with self.assertRaisesRegex(BackpropError, "outside the requested run"):
                sync(other.sync_dir, sink=self.store)
        self.assertEqual(len(self.store.events), before)

    def test_resolved_local_branch_resumes_online_with_new_metadata(self):
        self.parent()
        child = self.new("child", offline=True, source="parent?_step=1")
        child.log({"loss": 2})
        child.finish()
        sync(child.sync_dir, sink=self.store)
        resumed = wandb.Run(project="branches", entity="owner", id="child", resume="must",
            name="renamed-child", tags=["resumed"], dir=self.root / "offline",
            settings={"console": "off", "silent": True}, _sink=self.store, _deployment={})
        self.runs.append(resumed)
        self.assertEqual(resumed.starting_step, 3)
        resumed.log({"loss": 1})
        resumed.finish()
        state = self.state(resumed)
        self.assertEqual([row["loss"] for row in state["history"]], [8, 4, 2, 1])
        self.assertEqual(state["metadata"]["name"], "renamed-child")
        self.assertEqual(state["metadata"]["tags"], ["resumed"])
        self.assertEqual(state["summary"]["loss"]["mean"], 3.75)
        self.assertEqual(resumed._journal.ack, resumed._journal.count)

    def test_later_online_rewind_rebuilds_aggregate_samples_for_both_branch_modes(self):
        for rewind_first in (False, True):
            with self.subTest(initial_rewind=rewind_first):
                parent_id = "rewind-parent" if rewind_first else "fork-parent"
                child_id = parent_id if rewind_first else "fork-child"
                self.parent(id=parent_id)
                child = self.new(child_id, offline=True, source=f"{parent_id}?_step=1", rewind=rewind_first)
                child.log({"loss": 2})
                child.log({"loss": 1})
                child.finish()
                sync(child.sync_dir, sink=self.store)
                resumed = wandb.Run(project="branches", entity="owner", id=child_id,
                    dir=self.root / "offline", settings={"console": "off", "silent": True,
                    "resume_from": f"{child_id}?_step=0"}, _sink=self.store, _deployment={})
                self.runs.append(resumed)
                resumed.log({"loss": 10})
                resumed.finish()
                state = self.state(resumed)
                self.assertEqual([row["loss"] for row in state["history"]], [8, 10])
                self.assertEqual(state["summary"]["loss"], {"mean": 9.0, "min": 8})
                empty = wandb.Run(project="branches", entity="owner", id=child_id,
                    dir=self.root / "offline", settings={"console": "off", "silent": True,
                    "resume_from": f"{child_id}?_step=0"}, _sink=self.store, _deployment={})
                self.runs.append(empty)
                empty.finish()
                state = self.state(empty)
                self.assertEqual([row["loss"] for row in state["history"]], [8])
                self.assertEqual(state["summary"]["loss"], {"mean": 8.0, "min": 8})

    def test_crash_after_remote_ack_recovers_without_republishing(self):
        from backprop import deferred_branch
        self.parent()
        child = self.new("child", offline=True, source="parent?_step=1")
        child.log({"loss": 2})
        child.finish()
        commit = deferred_branch._atomic_json
        def fail_raw_cursor(path, value):
            if path.name == "ack.json":
                raise OSError("crash before original cursor update")
            return commit(path, value)
        with patch.object(deferred_branch, "_atomic_json", side_effect=fail_raw_cursor):
            with self.assertRaisesRegex(OSError, "crash before original"):
                sync(child.sync_dir, sink=self.store)
        accepted = list(self.store.events)
        with patch.object(self.store, "publish", side_effect=AssertionError("already acknowledged")):
            self.assertEqual(sync(child.sync_dir, sink=self.store), child._journal.count)
        self.assertEqual(self.store.events, accepted)

    def test_partial_ack_counts_only_original_records_after_prefix(self):
        self.parent()
        child = self.new("child", offline=True, source="parent?_step=1")
        for index in range(120):
            child.log({"loss": index})
        child.finish()
        publish, calls = self.store.publish, []
        def fail_second(events):
            calls.append(events)
            if len(calls) == 2:
                raise OSError("second batch interrupted")
            publish(events)
        with patch.object(self.store, "publish", side_effect=fail_second):
            with self.assertRaises(BackpropError):
                sync(child.sync_dir, sink=self.store)
        original_ids = {json.loads(line)["event_id"] for line in (Path(child.sync_dir) / "events.jsonl").read_text().splitlines()}
        accepted_raw = sum(event["event_id"] in original_ids for event in self.store.read_events(run_id=child.id))
        raw_ack = json.loads((Path(child.sync_dir) / "ack.json").read_text())["count"]
        self.assertGreater(raw_ack, 0)
        self.assertEqual(raw_ack, accepted_raw)
        self.assertLess(raw_ack, child._journal.count)
        sync(child.sync_dir, sink=self.store)
        self.assertEqual(len(self.state(child)["history"]), 122)

    def test_failed_parent_read_does_not_commit_snapshot_or_publish(self):
        self.parent()
        child = self.new("child", offline=True, source="parent?_step=1")
        child.finish()
        before = list(self.store.events)
        with patch.object(self.store, "read_events", side_effect=ConnectionError("read interrupted")):
            with self.assertRaises(ConnectionError):
                sync(child.sync_dir, sink=self.store)
        self.assertFalse((Path(child.sync_dir) / "branch-parent.json").exists())
        self.assertEqual(before, self.store.events)
        sync(child.sync_dir, sink=self.store)
        self.assertEqual(len(self.state(child)["history"]), 2)

    def test_external_sync_acknowledgement_does_not_mutate_writer_offsets(self):
        self.parent()
        child = self.new("child", offline=True, source="parent?_step=1")
        child.log({"loss": 2})
        child.finish()
        self.assertEqual(child._journal.acknowledged_count(), 0)
        sync(child.sync_dir, sink=self.store)
        self.assertEqual(child._journal.acknowledged_count(), child._journal.count)
        self.assertEqual(child._journal.ack, 0)
        self.assertEqual(child._journal._byte_offset, 0)
        self.assertGreater(json.loads((Path(child.sync_dir) / "branch-ack.json").read_text())["count"], child._journal.count)
        for invalid in (-1, True, "2", child._journal.count + 10000):
            with self.subTest(cursor=invalid):
                child._journal.ack_path.write_text(json.dumps({"count": invalid}))
                with self.assertRaises(BackpropError):
                    child._journal.acknowledged_count()

    def test_concurrent_sync_and_two_children_keep_distinct_stable_prefixes(self):
        self.parent()
        children = [self.new(name, offline=True, source="parent?_step=0") for name in ("one", "two")]
        for child in children:
            child.log({"loss": 2})
            child.finish()
        errors = []
        def upload(child):
            try: sync(child.sync_dir, sink=self.store)
            except BaseException as error: errors.append(error)
        threads = [threading.Thread(target=upload, args=(child,)) for child in children for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        ids = [event["event_id"] for event in self.store.events]
        self.assertEqual(len(ids), len(set(ids)))
        for child in children:
            self.assertEqual([row["loss"] for row in self.state(child)["history"]], [8, 2])


if __name__ == "__main__":
    unittest.main()
