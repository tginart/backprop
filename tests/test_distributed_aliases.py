"""Preserve contributor aliases through completion and ordinary later edits.

Architecture: independent Run contributions -> frozen completion -> public alias
lookup from each producer -> descriptive updates and a later disjoint generation.
The process-exit fixture separately shares these alias views with browser replay.
Main caller: SDK release suite. Existing stream and immutable file paths suffice.
"""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from test_artifact_identity import SETTINGS


class DistributedAliases(unittest.TestCase):
    def test_all_contributor_aliases_survive_completion_and_later_edits(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
            sink=w.MemorySink()
            def run(identifier):
                return w.init(id=identifier,entity='e',project='p',group='g',dir=directory,
                    _sink=sink,_deployment={},settings=SETTINGS,reinit='create_new')
            with run('worker') as worker:
                part=w.Artifact('model','model')
                with part.new_file('weights') as output:output.write('weights')
                worker.upsert_artifact(part,aliases=['best','worker'])
                worker.upsert_artifact(part,aliases=['worker-second'])
            with run('primary') as primary:
                complete=primary.finish_artifact(w.Artifact('model','model'),aliases=['release'])
            api=w.Api._from_store(sink,entity='e',project='p')
            expected={'best','worker','worker-second','release','latest'}
            for alias in expected:self.assertEqual(api.artifact('model:'+alias).id,complete.id)
            self.assertIs(part.wait(),part);self.assertEqual(set(part.aliases),expected)
            for name in ['worker','primary']:
                self.assertEqual(set(api.run(name).logged_artifacts()[0].aliases),expected)
            identity,version=complete.id,complete.version
            part.aliases=['worker','release'];part.metadata={'checked':True};part.save()
            self.assertEqual((part.id,part.version),(identity,version))
            self.assertEqual(api.artifact(identity).metadata,{'checked':True})
            with self.assertRaises(w.CommError):api.artifact('model:best')
            with run('later-worker') as later:
                later.upsert_artifact(w.Artifact('model','model'),aliases=['best'])
            with run('later-primary') as later:
                next_artifact=later.finish_artifact(w.Artifact('model','model'))
            self.assertNotEqual(next_artifact.id,identity)
            self.assertEqual(api.artifact('model:best').id,next_artifact.id)
            self.assertEqual(api.artifact('model:worker').id,identity)
            self.assertEqual(api.artifact('model:release').version,version)
