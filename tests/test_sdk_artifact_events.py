"""Implicit Table outputs use the same immutable readers as explicit outputs.

Architecture: real Run/Journal writes -> MemorySink -> public artifact and native
URL reads; deferred branches prove that value ownership controls inheritance.
Small pure fixtures cover event identity and malformed input independently of
the writer. No external credentials, services or optional packages are needed.
Main callers: unittest discovery and release verification.
"""
import copy
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.artifact_events import iter_artifact_events
from backprop.apis.public.replay import reduce_events
from backprop.deferred_branch import _source_records
from backprop.transport import MemorySink, read_journal, sync


class ArtifactEventIdentity(unittest.TestCase):
    def event(self, id, kind, payload):
        return {"schema_version": 1, "event_id": id, "timestamp": "2026-10-03T00:00:00Z",
                "run_id": "r", "entity": "owner", "project": "project", "type": kind, "payload": payload}

    def test_order_source_step_dedup_and_immutable_payload(self):
        manifest = {"artifact_id": "table", "name": "run-r-table", "files": []}
        carrier = self.event("row", "history", {"step": 7, "metrics": {}, "artifacts": [manifest, manifest]})
        explicit = self.event("explicit", "artifact", {"artifact_id": "model"})
        events = [explicit, carrier, copy.deepcopy(carrier)]
        before = copy.deepcopy(events)
        outputs = list(iter_artifact_events(events))
        self.assertEqual([item["payload"]["artifact_id"] for item in outputs], ["model", "table"])
        implicit = outputs[1]
        self.assertEqual((implicit["source_event_id"], implicit["source_event_type"], implicit["source_event_step"]),
                         ("row", "history", 7))
        for key in ("timestamp", "schema_version", "run_id", "entity", "project"):
            self.assertEqual(implicit[key], carrier[key])
        self.assertEqual(implicit["payload"], manifest)
        self.assertEqual(events, before)
        self.assertEqual(list(iter_artifact_events(events)), outputs)

    def test_distinct_carriers_reject_conflicting_immutable_ids(self):
        first = self.event("a", "summary", {"artifacts": [{"artifact_id": "same", "name": "first"}]})
        second = self.event("b", "config", {"artifacts": [{"artifact_id": "same", "name": "second"}]})
        same = self.event("b", "config", copy.deepcopy(first["payload"]))
        outputs = list(iter_artifact_events([first, same]))
        self.assertEqual(len(outputs), 2)
        self.assertNotEqual(outputs[0]["event_id"], outputs[1]["event_id"])
        self.assertNotIn("source_event_step", outputs[0])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            list(iter_artifact_events([first, second]))
        for invalid in ({"artifacts": {}}, {"artifacts": [{}]},
                        {"artifacts": [{"artifact_id": "same", "name": "first"},
                                       {"artifact_id": "same", "name": "second"}]}):
            with self.subTest(payload=invalid), self.assertRaises(ValueError):
                list(iter_artifact_events([self.event("invalid", "summary", invalid)]))
        changed = copy.deepcopy(first)
        changed["payload"]["artifacts"][0]["name"] = "changed"
        with self.assertRaisesRegex(ValueError, "Conflicting records"):
            list(iter_artifact_events([first, changed]))

    def test_replay_checks_conflicts_but_accepts_same_verified_files_at_other_locations(self):
        manifest = {"artifact_id": "same", "metadata": {"purpose": "original"},
                    "files": [{"name": "table.json", "path": "original/table.json", "dataset_rid": "one",
                               "sha256": "a" * 64, "size": 10, "digest": "checksum", "extra": {}}]}
        first = self.event("a", "summary", {"values": {}, "artifacts": [manifest]})
        replica = self.event("b", "artifact", copy.deepcopy(manifest))
        replica["payload"]["files"][0].update(path="copied/table.json", dataset_rid="two", branch="new")
        replica["payload"].update(created_at="another timestamp", history_step=99)
        self.assertEqual(len(reduce_events([first, replica])["artifacts"]), 1)
        for changed in ("metadata", "digest", "sha256", "size"):
            conflict = copy.deepcopy(replica)
            if changed == "metadata":
                conflict["payload"][changed] = {"purpose": "changed"}
            elif changed == "size":
                conflict["payload"]["files"][0][changed] = 11
            else:
                conflict["payload"]["files"][0][changed] = "different"
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "ambiguous"):
                reduce_events([first, conflict])

    def test_branch_selection_preserves_config_and_summary_carrier_contract(self):
        def carrier(id, kind, **payload):
            return self.event(id, kind, {**payload, "artifacts": [{"artifact_id": id}]})
        events = [carrier("config", "config", values={}), carrier("summary", "summary", values={}),
                  carrier("kept", "history", step=0, metrics={}),
                  carrier("future", "history", step=1, metrics={}),
                  carrier("future-summary", "summary", values={})]
        selected = list(iter_artifact_events(_source_records(events, 0)))
        self.assertEqual([event["payload"]["artifact_id"] for event in selected], ["config", "summary", "kept"])


class EmbeddedArtifactReaders(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MemorySink()
        self.runs = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.settings = {"console": "off", "silent": True, "disable_git": True,
                         "flush_interval": 3600}
        self.api = backprop.Api._from_store(self.store, entity="owner", project="outputs")

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        self.environment.stop()
        self.temporary.cleanup()

    def new(self, id, *, offline=False, **kwargs):
        settings = {**self.settings, **kwargs.pop("settings", {})}
        run = backprop.Run(project="outputs", entity="owner", id=id,
            dir=kwargs.pop("dir", self.root / ("offline" if offline else "online")),
            mode="offline" if offline else "online", settings=settings,
            _sink=kwargs.pop("sink", None if offline else self.store), _deployment={}, **kwargs)
        self.runs.append(run)
        return run

    def test_public_queries_lineage_native_url_and_duplicate_outputs(self):
        run = self.new("writer")
        table = backprop.Table(columns=["x"], data=[[1], [2]])
        run.log({"table ?#雪": table})
        run.log({"table ?#雪": table})
        run.finish()
        events = self.store.read_events(run_id="writer")
        outputs = list(iter_artifact_events(events))
        self.assertEqual(len(outputs), 1)
        manifest = outputs[0]["payload"]
        artifact = self.api.artifact(manifest["artifact_id"])
        self.assertEqual(artifact.get("table ?#雪").data, [[1], [2]])
        self.assertEqual(artifact.logged_by().id, run.id)
        self.assertEqual(self.api.artifact(artifact.qualified_name).id, artifact.id)
        self.assertIn("run_table", [kind.name for kind in self.api.artifact_types()])
        self.assertEqual([item.id for item in self.api.artifacts("run_table", manifest["name"])], [artifact.id])
        target = backprop.Artifact("copy", "dataset")
        target._store = self.store
        source_entry = artifact.get_entry("table ?#雪.table.json")
        copied = target.add_reference(source_entry.ref_url())[0]
        self.assertEqual(copied._read_verified_bytes(), source_entry._read_verified_bytes())
        consumer = self.new("consumer")
        consumer.use_artifact(artifact)
        consumer.finish()
        self.assertEqual([item.id for item in artifact.used_by()], [consumer.id])
        self.assertEqual([item.id for item in self.api.run("consumer").used_artifacts()], [artifact.id])
        # A manifest may also be inherited explicitly while its owning value is
        # retained. One run's output listing must still show one immutable ID.
        duplicate = {**outputs[0], "event_id": "inherited-explicit"}
        self.store.events.extend([copy.deepcopy(events[0]), duplicate])
        self.assertEqual([item.id for item in self.api.run("writer").logged_artifacts()], [artifact.id])

    def test_embedded_manifest_hydrates_config_id_resume_and_local_fork(self):
        source = self.new("source")
        source.log({"table": backprop.Table(columns=["x"], data=[[5]])})
        source.flush()
        manifest = list(iter_artifact_events(self.store.read_events(run_id="source")))[0]["payload"]
        source.config.update({"dataset": {"_type": "artifactVersion", "id": manifest["artifact_id"]}})
        source.finish()
        resumed = self.new("source", resume="must")
        self.assertEqual(resumed.config.dataset.get("table").data, [[5]])
        resumed.finish()
        child = self.new("child", settings={"fork_from": "source?_step=0"})
        child.finish()
        shutil.rmtree(source.sync_dir)
        local = self.new("child", resume="must", dir=self.root / "online")
        self.assertEqual(local.config.dataset.get("table").data, [[5]])
        self.assertEqual(local.config.dataset.id, manifest["artifact_id"])
        local.finish()

    def test_deferred_branch_keeps_selected_history_config_summary_outputs(self):
        parent = self.new("parent")
        kept = backprop.Table(columns=["x"], data=[[0]])
        parent.log({"keep": kept})
        parent.config.update({"dataset": kept._artifact_target})
        parent.summary["manual_table"] = backprop.Table(columns=["x"], data=[[4]])
        parent.log({"discard": backprop.Table(columns=["x"], data=[[1]])})
        explicit = backprop.Artifact("explicit-after-cut", "dataset")
        explicit.add(backprop.Table(columns=["x"], data=[[9]]), "table")
        parent.log_artifact(explicit)
        parent.finish()
        source = self.store.read_events(run_id="parent")
        source_before = copy.deepcopy(source)
        selected = _source_records(source, 0)
        expected = list(iter_artifact_events(selected))
        self.assertEqual(len(expected), 3)
        self.assertEqual({event["source_event_type"] for event in expected if "source_event_type" in event},
                         {"summary", "history"})
        child = self.new("deferred", offline=True, settings={"fork_from": "parent?_step=0"})
        child.log({"loss": 2})
        child.finish()
        sync(child.sync_dir, sink=self.store)
        child_events = self.store.read_events(run_id="deferred")
        actual = list(iter_artifact_events(child_events))
        self.assertEqual({item["payload"]["artifact_id"] for item in actual},
                         {item["payload"]["artifact_id"] for item in expected})
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(source, source_before)
        self.assertEqual(len(self.api.run("deferred").logged_artifacts()), 3)
        self.assertEqual(self.api.run("deferred").config["dataset"]["artifact_id"], kept._artifact_target.id)
        for output in actual:
            self.assertEqual(output["run_id"], "deferred")
            if output.get("source_event_type") == "history":
                self.assertEqual(output["source_event_step"], 0)
            original = next(item for item in expected if item["payload"]["artifact_id"] == output["payload"]["artifact_id"])
            self.assertEqual(output["payload"], original["payload"])
        self.assertEqual([row["_step"] for row in reduce_events(child_events)["history"]], [0, 1])
        self.assertEqual(len(list(iter_artifact_events(read_journal(child.sync_dir)))), 3)


if __name__ == "__main__":
    unittest.main()
