"""Alias property parity and actual event-backed ownership changes.

Architecture: isolated pinned/native comparison -> durable publication/recovery
-> independent API lookup and browser replay. Main callers: SDK suite and CI.
Backend version names remain content based; version-like aliases are explicit
user bindings, never automatically assigned W&B sequential version numbers.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class ArtifactAliasContracts(unittest.TestCase):
    def test_pinned_alias_getter_setter_signatures_and_logging_preparation(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel is unavailable')
        self.assertEqual(reference.version,'0.30.0');records=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_alias_reference_probe.py'),mode],
                cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=45,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);records.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                folder=ROOT/'test-results';folder.mkdir(exist_ok=True)
                (folder/f'artifact-alias-{mode}.json').write_text(result.stdout)
        self.assertEqual(records[0]['imports'],records[1]['imports'])
        self.assertEqual(records[0]['logging_aliases'],records[1]['logging_aliases'])
        self.assertEqual(records[0]['use_existing'],records[1]['use_existing'])
        cases=records[1]['imports']['wandb']
        self.assertEqual(cases['committed/duplicates']['value'],['best','best','latest','best'])
        self.assertEqual(cases['committed/mutation']['value'],{'aliases':['best','inplace'],'input':['best','external'],'same_object':True})
        self.assertEqual(cases['committed/empty_alias']['value'],[''])
        self.assertEqual(cases['committed/version_alias']['value'],['v1'])
        self.assertEqual(cases['queued/get']['error'],'ArtifactNotLoggedError')
        self.assertEqual(records[1]['logging_aliases']['empty']['value'],['latest'])
        self.assertEqual(records[1]['logging_aliases']['string']['value'],['best','latest'])

    def test_publication_ack_gate_and_offline_sync_from_another_reader(self):
        from backprop.artifact_identity import ArtifactNotLoggedError
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
            sink=w.MemorySink()
            with w.init(id='queued',entity='e',project='aliases',mode='offline',dir=directory,_deployment={},settings=SETTINGS) as run:
                artifact=w.Artifact('dataset','data');run.log_artifact(artifact,aliases=['best'])
                with self.assertRaises(ArtifactNotLoggedError):_ = artifact.aliases
                with self.assertRaises(ArtifactNotLoggedError):artifact.aliases=['after']
            w.sync(run.sync_dir,sink=sink)
            self.assertEqual(set(artifact.aliases),{'best','latest'})
            artifact.aliases=['', 'v1','best','best']
            self.assertEqual(artifact.aliases,['', 'v1','best','best'])

    def test_durable_alias_ownership_and_immutable_version_precedence(self):
        from artifact_alias_replay_fixture import build
        fixture=build()
        self.assertEqual(fixture['resolved']['latest'],fixture['second_id'])
        self.assertEqual(fixture['resolved']['best'],fixture['second_id'])
        self.assertEqual(fixture['resolved'][''],fixture['first_id'])
        self.assertEqual(fixture['resolved']['v1'],fixture['first_id'])
        self.assertEqual(fixture['resolved'][fixture['first_version']],fixture['first_id'])
        self.assertTrue(fixture['mutation_restart_recovered'])
        self.assertEqual(fixture['files'],{'first':'first','second':'second'})

    def test_using_committed_artifacts_does_not_edit_their_aliases(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
            sink=w.MemorySink()
            with w.init(id='use',entity='e',project='aliases',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
                artifact=w.Artifact('dataset','data');run.log_artifact(artifact,aliases=['best']);run.flush()
                before=list(artifact.aliases)
                self.assertIs(run.use_artifact(artifact,aliases=['other']),artifact)
                self.assertEqual(artifact.aliases,before)
                found=run.use_artifact('dataset:best',aliases=['not/a/valid/alias'])
                self.assertEqual(found.id,artifact.id)
                self.assertEqual(set(found.aliases),set(before))
            self.assertFalse(any(e['type']=='artifact_update' for e in sink.events))
