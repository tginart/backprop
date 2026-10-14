"""Checkpoint recovery uses immutable journal records, including process restart.

Architecture: framework-neutral checkpoint bytes -> adapter helper -> public
artifact reads and separate-process replay. Main callers: core SDK CI and local
regression tests; actual framework dispatch is covered by test_optional_fabric.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api
from backprop.integration.checkpoint_artifacts import log_checkpoint
from backprop.transport import MemorySink, read_journal

ROOT = Path(__file__).resolve().parents[1]


class CheckpointRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {}, clear=True); self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = MemorySink(); self.directory = Path(self.temp.name)
        self.settings = {'silent': True, 'console': 'off', 'x_disable_stats': True,
                         'disable_git': True, 'flush_interval': 600}
        self.run = backprop.Run(entity='e', project='p', id='checkpoint', dir=self.temp.name,
                               settings=self.settings, _sink=self.store, _deployment={})
        self.addCleanup(self.run.finish)
        self.api = Api._from_store(self.store, entity='e', project='p')
        self.path = self.directory/'checkpoint.bin'; self.path.write_bytes(b'original')

    def artifact(self, score=1):
        artifact = backprop.Artifact('weights', 'model', metadata={'score': score})
        artifact.add_file(self.path, name='model.ckpt')
        return artifact

    def test_lost_ack_reuses_identity_and_aliases_without_replacing_contents(self):
        original = self.run.log_model
        def failed(*args, **kwargs):
            original(*args, **kwargs); raise OSError('lost acknowledgement')
        with patch.object(self.run, 'log_model', side_effect=failed):
            with self.assertRaises(OSError): log_checkpoint(self.run, self.artifact(), ['latest'])
        log_checkpoint(self.run, self.artifact(), ['latest', 'best'])
        self.run.flush()
        artifacts = list(self.api.run(self.run.id).logged_artifacts())
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].get_entry('model.ckpt')._read_verified_bytes(), b'original')
        self.assertEqual(self.api.artifact('weights:best').id, artifacts[0].id)
        self.assertEqual(len([e for e in read_journal(self.run.sync_dir) if e['type']=='artifact']), 1)
        self.path.write_bytes(b'changed')
        log_checkpoint(self.run, self.artifact(), ['latest'])
        self.run.flush()
        self.assertEqual(len(list(self.api.run(self.run.id).logged_artifacts())), 2)
        self.assertNotEqual(self.api.artifact('weights:latest').id, artifacts[0].id)
        self.assertEqual(self.api.artifact('weights:best').id, artifacts[0].id)

    def test_restart_in_a_fresh_process_recovers_from_durable_manifest(self):
        log_checkpoint(self.run, self.artifact(), ['latest']); self.run.finish()
        original = self.api.artifact('weights:latest')
        code = '''import json, sys
from pathlib import Path
import backprop
from backprop.integration.checkpoint_artifacts import log_checkpoint
from backprop.transport import MemorySink, read_journal
root=sys.argv[1]
run=backprop.Run(entity='e',project='p',id='checkpoint',dir=root,resume='allow',
 settings={'silent':True,'console':'off','x_disable_stats':True,'disable_git':True},
 _sink=MemorySink(),_deployment={})
artifact=backprop.Artifact('weights','model',metadata={'score':1})
artifact.add_file(Path(root)/'checkpoint.bin',name='model.ckpt')
log_checkpoint(run,artifact,['best']); run.finish()
events=read_journal(run.sync_dir)
print(json.dumps({'ids':[e['payload']['artifact_id'] for e in events if e['type']=='artifact'],
 'updates':[e['payload'] for e in events if e['type']=='artifact_update']}))
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, self.temp.name], capture_output=True,
            text=True, timeout=30, env={'PATH': os.defpath, 'PYTHONPATH': str(ROOT/'sdk')})
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed['ids'], [original.id])
        self.assertEqual(observed['updates'][-1]['artifact_id'], original.id)
        self.assertEqual(set(observed['updates'][-1]['alias_add']), {'best', 'latest'})

    def test_changed_metadata_is_distinct_and_repeated_uses_list_one_artifact(self):
        log_checkpoint(self.run, self.artifact(1), ['first'])
        log_checkpoint(self.run, self.artifact(2), ['second']); self.run.flush()
        first = self.api.artifact('weights:first'); second = self.api.artifact('weights:second')
        self.assertNotEqual(first.id, second.id)
        self.run.use_artifact(first, use_as='training')
        self.run.use_artifact(first, use_as='validation')
        self.run.use_artifact(second)
        self.run.flush()
        self.store.events = list(reversed(self.store.events)) + self.store.events
        self.assertEqual([a.id for a in self.api.run(self.run.id).used_artifacts()], [first.id, second.id])

    def test_invalid_aliases_and_failed_append_do_not_mark_checkpoint_committed(self):
        with self.assertRaises(ValueError): log_checkpoint(self.run, self.artifact(), ['bad:alias'])
        with patch.object(self.run._journal, 'append', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError): log_checkpoint(self.run, self.artifact(), ['latest'])
        self.assertFalse(any(e['type']=='artifact' for e in read_journal(self.run.sync_dir)))
        log_checkpoint(self.run, self.artifact(), ['latest']); self.run.flush()
        self.assertEqual(len(list(self.api.run(self.run.id).logged_artifacts())), 1)


if __name__ == '__main__': unittest.main()
