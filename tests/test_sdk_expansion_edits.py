"""Experiment edits against durable SDK journals, including failed acknowledgements.

Architecture: logging fixtures -> public edits -> independent reader and browser
fixture. Tests cover user-observable persistence, version integrity, and retries.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api, Run
from backprop.transport import MemorySink


class Edits(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BACKPROP_DIR": self.temp.name})
        self.env.start()
        self.store = MemorySink()
        self.api = Api._from_store(self.store, entity="team", project="project")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def writer(self, id="one"):
        return backprop.Run(id=id, entity="team", project="project", _sink=self.store,
                            dir=self.temp.name, settings={"console": "off", "silent": True})

    def test_run_edits_and_nested_summary_survive_fresh_reader(self):
        run = self.api.create_run(run_id="one")
        run.config["optimizer"] = {"lr": .1}
        run.name = "readable"
        run.tags = ["baseline"]
        run.tags.append("reviewed")
        run.update()
        run.summary["eval"] = {"accuracy": .8}
        run.summary.eval.accuracy = .95
        run.summary["keep"] = 12
        fresh = self.api.run("one")
        self.assertEqual(fresh.summary.eval.accuracy, .95)
        self.assertEqual(fresh.config, {"optimizer": {"lr": .1}})
        self.assertEqual(fresh.name, "readable")
        self.assertEqual(fresh.tags, ["baseline", "reviewed"])
        self.assertTrue(fresh.update_state("pending"))
        self.assertTrue(fresh.update_state("failed"))
        self.assertEqual(self.api.run("one").state, "failed")
        del fresh.summary["eval"]
        self.assertEqual(dict(self.api.run("one").summary), {"keep": 12})

    def test_project_without_runs_and_pending_creation(self):
        self.api.create_project("empty", "team")
        self.assertEqual([p.name for p in self.api.projects()], ["empty"])
        self.assertEqual(len(self.api.runs("empty")), 0)
        run = Run.create(self.api, "queued", state="pending")
        self.assertEqual(run.state, "pending")
        self.assertEqual(self.api.run("queued").state, "pending")

    def test_file_upload_and_rejection_outside_root(self):
        run = self.api.create_run(run_id="one")
        path = Path(self.temp.name) / "data.txt"
        path.write_text("hello")
        result = run.upload_file(str(path), self.temp.name)
        self.assertEqual(result.name, "data.txt")
        self.assertEqual(self.store.download_file(run._state["files"]["data.txt"]), b"hello")
        with self.assertRaises(Exception): run.upload_file(str(path), str(path / "child"))

    def test_alias_reassignment_metadata_and_file_identity(self):
        with self.writer() as run:
            artifacts = []
            for i in range(2):
                a = backprop.Artifact("weights", "model", metadata={"iteration": i})
                with a.new_file("weights.txt") as f: f.write(str(i))
                artifacts.append(run.log_artifact(a, aliases=["latest", f"epoch{i}"]))
        first, second = artifacts
        self.assertEqual(self.api.artifact("weights:latest").id, second.id)
        self.assertEqual(self.api.artifact("weights:epoch0").id, first.id)
        edit = self.api.artifact(first.id)
        version = edit.version
        edit.metadata["evaluation"] = .9
        edit.aliases.append("best")
        edit.tags = ["approved"]
        edit.save()
        fresh = self.api.artifact("weights:best")
        self.assertEqual(fresh.version, version)
        self.assertEqual(fresh.metadata["evaluation"], .9)
        self.assertEqual(fresh.tags, ["approved"])
        self.assertEqual(self.api.artifact("weights:latest").id, second.id)
        self.assertEqual(fresh.get_entry("weights.txt")._read_verified_bytes(), b"0")
        collection = self.api.artifact_collection("model", "weights")
        collection.description = "Model weights"
        collection.tags.append("collection-tag")
        collection.save()
        self.assertEqual(self.api.artifact_collection("model", "weights").description, "Model weights")
        self.assertIn("collection-tag", self.api.artifact_collection("model", "weights").tags)
        collection.name = "renamed"
        collection.save()
        self.assertEqual(self.api.artifact("renamed:best").id, first.id)
        self.assertEqual(self.api.artifact_collection("model", "renamed").id, collection.id)

    def test_acknowledgement_failure_reuses_event_identity(self):
        run = self.api.create_run(run_id="one")
        publish = self.store.publish
        def fail(events):
            publish(events)
            raise OSError("connection lost after publish")
        with patch.object(self.store, "publish", side_effect=fail):
            with self.assertRaises(backprop.BackpropError): run.summary["score"] = 4
        run.summary["score"] = 4
        updates = [e for e in self.store.events if e["type"] == "summary"]
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0]["event_id"], updates[1]["event_id"])
        self.assertEqual(self.api.run("one").summary["score"], 4)

    def test_delayed_visibility_keeps_local_edits_and_pending_config(self):
        run = self.api.create_run(run_id="one")
        snapshot = list(self.store.events)
        run.config["pending"] = 7
        run.name = "pending name"
        run.summary["review"] = "ready"
        self.assertEqual(run.config["pending"],7)
        self.assertEqual(run.name,"pending name")
        run.update()
        with patch.object(self.store,"read_events",return_value=snapshot):
            run.load(force=True)
            self.assertEqual(run.summary["review"],"ready")
            self.assertEqual(run.config["pending"],7)
        self.assertEqual(self.api.run("one").name,"pending name")

    def test_failed_mutation_recovers_from_journal_after_restart(self):
        from backprop.transport import sync
        run=self.api.create_run(run_id='one')
        with patch.object(self.store,'publish',side_effect=OSError('offline')):
            with self.assertRaises(backprop.BackpropError):run.summary['recovered']=True
        journals=list((Path(self.temp.name)/'mutations').glob('*/events.jsonl'))
        for journal in journals:sync(journal.parent,sink=self.store)
        fresh=Api._from_store(self.store,entity='team',project='project')
        self.assertIs(fresh.run('one').summary['recovered'],True)

    def test_public_artifact_attachments_validate_before_writing(self):
        with self.writer() as run:
            artifact=backprop.Artifact('weights','model')
            with artifact.new_file('weights.bin') as f:f.write('weights')
            run.log_artifact(artifact,aliases='best')
        public=self.api.create_run(run_id='consumer')
        before=len(self.store.events)
        with self.assertRaises(Exception):public.log_artifact(artifact,aliases=['bad/name'])
        with self.assertRaises(Exception):public.log_artifact(artifact,tags=['bad!'])
        self.assertEqual(len(self.store.events),before)
        self.assertIs(public.log_artifact(artifact,aliases=['release'],tags='approved'),artifact)
        self.assertIs(public.use_artifact(artifact,use_as='input'),artifact)
        self.api.flush()
        fresh=self.api.run('consumer')
        self.assertEqual([a.id for a in fresh.logged_artifacts()],[artifact.id])
        self.assertEqual([a.id for a in fresh.used_artifacts()],[artifact.id])
        edit=self.api.artifact('weights:release')
        edit.tags.append('reviewed');edit.save();self.api.flush()
        self.assertEqual(self.api.artifact('weights:release').tags,['approved','reviewed'])
        edit.tags.append('invalid!')
        before=len(self.store.events)
        with self.assertRaises(ValueError):edit.save()
        self.assertEqual(len(self.store.events),before)

    def test_stale_alias_removal_cannot_erase_reassigned_owner(self):
        with self.writer() as run:
            a=backprop.Artifact('weights','model')
            with a.new_file('a') as f:f.write('one')
            run.log_artifact(a,aliases=['best'])
            run.flush()
            stale=self.api.artifact('weights:best')
            b=backprop.Artifact('weights','model')
            with b.new_file('a') as f:f.write('two')
            run.log_artifact(b,aliases=['best'])
        stale.aliases.remove('best');stale.metadata['review']='old';stale.save()
        self.api.flush()
        self.assertEqual(self.api.artifact('weights:best').id,b.id)

    def test_model_helpers_paths_aliases_and_return_values(self):
        path=Path(self.temp.name)/'checkpoint.bin';path.write_bytes(b'model')
        with self.writer() as run:
            self.assertIsNone(run.log_model(str(path),name='checkpoint',aliases=['best']))
            run.flush()
            artifact=run.use_artifact('checkpoint:best',type='model',use_as='start')
            self.assertEqual(artifact.type,'model')
            old=os.getcwd()
            try:
                os.chdir(self.temp.name)
                downloaded=run.use_model('checkpoint:best')
                self.assertEqual(Path(downloaded).read_bytes(),b'model')
            finally:os.chdir(old)
            with self.assertRaises(backprop.CommError):run.use_artifact('checkpoint:best',type='dataset')
