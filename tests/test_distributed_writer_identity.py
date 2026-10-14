"""Keep distributed retry identity scoped to the actual logical writer.

Architecture: lost acknowledgments -> public independent shared handles -> exact
part identities and journal cursors. A child-process fixture verifies recovery
without surviving run objects and is replayed by the browser test as well.
Main caller: SDK regression suite. No credentials or external service is needed.
"""
import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from test_artifact_identity import SETTINGS


class DistributedWriterIdentity(unittest.TestCase):
    def test_identical_parts_from_unlabeled_handles_remain_distinct(self):
        for label in [None,'']:
            with self.subTest(label=label),tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
                sink=w.MemorySink()
                def run(label):return w.init(id='shared',entity='e',project='p',group='parts',mode='shared',dir=directory,
                    _sink=sink,_deployment={},settings={**SETTINGS,'x_label':label,'x_primary':False},reinit='create_new')
                def part():
                    a=w.Artifact('model','model')
                    with a.new_file('same') as out:out.write('same')
                    return a
                first=run(label);second=run(label);publish=sink.publish
                def lost(events):
                    publish(events)
                    if any(e['type']=='artifact_part' for e in events):raise OSError('lost acknowledgment')
                try:
                    with patch.object(sink,'publish',side_effect=lost):
                        with self.assertRaises(w.Error):first.upsert_artifact(part())
                    before=first._journal.acknowledged_count();second.upsert_artifact(part())
                    self.assertEqual(first._journal.acknowledged_count(),before)
                    parts={e['payload']['part_id']:e for e in sink.events if e['type']=='artifact_part'}
                    self.assertEqual(len(parts),2)
                    self.assertEqual({e['payload']['_writer']['id'] for e in parts.values()},{first._writer_id,second._writer_id})
                    first.upsert_artifact(part())
                    self.assertEqual(len({e['payload']['part_id'] for e in sink.events if e['type']=='artifact_part'}),2)
                finally:first.finish();second.finish()

    def test_generated_identity_cannot_collide_with_an_explicit_label(self):
        from types import SimpleNamespace
        from backprop.artifact_operations import fingerprint
        first=SimpleNamespace(entity='e',project='p',id='shared',_shared=True,_writer_id='same',_settings=SimpleNamespace(x_label=None))
        second=SimpleNamespace(entity='e',project='p',id='shared',_shared=True,_writer_id='different',_settings=SimpleNamespace(x_label='same'))
        self.assertNotEqual(fingerprint(first,'group',{}),fingerprint(second,'group',{}))

    def test_independent_process_exit_explicit_sync_and_labeled_restart(self):
        from distributed_writer_replay_fixture import build
        for label in [None,'','stable-worker']:
            with self.subTest(label=label):
                value=build(label);self.assertTrue(value['hard_exit_recovered'])
                self.assertEqual(value['distinct_contributions'],2 if label else 3)
