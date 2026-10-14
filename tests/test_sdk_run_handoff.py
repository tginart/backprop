"""Run transfer preserves identity, primary ownership and offline writer journals.

Architecture: native parent -> serialized descriptor -> independent subprocesses
-> shuffled event replay and primary restart. Main callers: core SDK CI; real
Lightning launch/serialization is tested separately in the optional matrix.
"""
from __future__ import annotations

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public.replay import reduce_events
from backprop.run_handoff import snapshot_run, restore_run
from backprop.transport import MemorySink, read_journal

ROOT = Path(__file__).resolve().parents[1]


class HandoffContracts(unittest.TestCase):
    def setUp(self):
        backprop.teardown()
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.addCleanup(backprop.teardown)
        self.environment=patch.dict(os.environ,{},clear=True); self.environment.start()
        self.addCleanup(self.environment.stop)
        self.sink=MemorySink(); self.runs=[]
        self.addCleanup(lambda:[run.finish() for run in self.runs if not run._closed])
        self.settings={'silent':True,'console':'off','x_disable_stats':True,'disable_git':True,'flush_interval':600}

    def writer(self, **options):
        args=dict(entity='e',project='handoff',id='same',dir=self.temp.name,_sink=self.sink,
                  _deployment={},settings=self.settings,config={'lr':.1})
        args.update(options)
        run=backprop.Run(**args);self.runs.append(run);return run

    def test_two_independent_workers_and_primary_finish_in_online_and_offline_modes(self):
        for mode in ['online','offline']:
            with self.subTest(mode=mode):
                parent=self.writer(id=mode,mode=mode,_sink=self.sink if mode=='online' else None)
                parent.define_metric('epoch');parent.define_metric('*',step_metric='epoch',step_sync=True)
                parent.log({'epoch':0,'parent':1})
                descriptor=snapshot_run(parent)
                path=Path(self.temp.name)/(mode+'.pickle');path.write_bytes(pickle.dumps(descriptor))
                processes=[]
                for label in ['rank0','rank1']:
                    output=Path(self.temp.name)/(mode+'-'+label+'.json')
                    process=subprocess.Popen([sys.executable,'-B',str(ROOT/'tests/handoff_worker.py'),str(path),label,str(output)],
                        env={'PATH':os.defpath,'PYTHONPATH':str(ROOT/'sdk')},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                    processes.append((process,output))
                events=[];writers=[]
                for process,output in processes:
                    stdout,stderr=process.communicate(timeout=30);self.assertEqual(process.returncode,0,stderr)
                    result=json.loads(output.read_text());events+=result['events'];writers.append(result['writer'])
                    self.assertEqual(result['scope'],['e','handoff',mode]);self.assertFalse(result['primary'])
                    self.assertEqual(result['mode'],mode);self.assertEqual(result['lr'],.1)
                    self.assertNotEqual(result['dir'],parent.sync_dir);self.assertEqual(result['step'],2)
                    first=min(datetime.fromisoformat(e['timestamp']) for e in result['events'])
                    self.assertGreater(first,datetime.fromisoformat(descriptor['timestamp']))
                self.assertEqual(len(set(writers)),2)
                before=read_journal(parent.sync_dir)+events
                self.assertEqual(reduce_events(before)['state'],'running')
                parent.log({'epoch':3,'parent':3});parent.finish()
                complete=read_journal(parent.sync_dir)+events
                state=reduce_events(complete)
                self.assertEqual(state,reduce_events(list(reversed(complete))+copy.deepcopy(complete)))
                self.assertEqual(state['state'],'finished')
                self.assertEqual(state['summary'],{'epoch':3,'parent':3,'rank0':2,'rank1':2})
                for writer in writers:
                    self.assertEqual([r['_step'] for r in state['history'] if r.get('_writer')==writer],[0,1])
                self.assertTrue(state['config']['rank0']);self.assertTrue(state['config']['rank1'])

    def test_matching_global_is_reused_but_unrelated_run_is_preserved(self):
        parent=self.writer();descriptor=snapshot_run(parent)
        with patch.object(backprop,'run',parent):self.assertIs(restore_run(descriptor),parent)
        other=self.writer(id='other')
        with patch.object(backprop,'run',other):
            child=restore_run(descriptor)
            self.assertIs(backprop.run,other);self.assertIsNot(child,parent);self.assertNotEqual(child.id,other.id)
            self.assertEqual(child.config.as_dict(), parent.config.as_dict())
            child.finish();self.assertFalse(other._closed);self.assertFalse(parent._closed)

    def test_primary_restart_preserves_writer_identity_sequence_and_other_summary(self):
        parent=self.writer();parent.log({'parent':1});descriptor=snapshot_run(parent)
        child=restore_run(descriptor);child.log({'child':2});child.finish()
        parent.log({'parent':3});parent.finish()
        old_writer=parent._writer_id;old_sequence=parent._writer_sequence
        resumed=self.writer(resume='allow',config=None)
        self.assertTrue(resumed._shared);self.assertEqual(resumed._writer_id,old_writer)
        self.assertGreaterEqual(resumed._writer_sequence,old_sequence)
        self.assertEqual(resumed.step,2)
        resumed.log({'after_restart':4});resumed.finish()
        state=reduce_events(self.sink.events)
        self.assertEqual(state['summary']['child'],2)
        self.assertEqual(state['summary']['after_restart'],4)
        sequences=[e['payload']['_writer']['sequence'] for e in self.sink.events if e['payload'].get('_writer',{}).get('id')==old_writer]
        self.assertEqual(len(sequences),len(set(sequences)))
        self.assertEqual(state['state'],'finished')

    def test_config_artifact_files_are_copied_to_offline_child_journal(self):
        parent=self.writer(mode='offline',_sink=None)
        value=backprop.Artifact('inputs','dataset')
        with value.new_file('data.txt') as stream:stream.write('input bytes')
        parent.log_artifact(value);parent.config.update({'inputs':value})
        descriptor=pickle.loads(pickle.dumps(snapshot_run(parent)))
        child=restore_run(descriptor)
        self.assertEqual(child.config['inputs'].get_entry('data.txt')._read_verified_bytes(),b'input bytes')
        self.assertTrue(any(e['type']=='artifact' for e in read_journal(child.sync_dir)))
        child.finish()

    def test_failed_marker_does_not_promote_parent_and_unknown_descriptor_is_rejected(self):
        parent=self.writer()
        with patch.object(parent._journal,'append',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):snapshot_run(parent)
        self.assertFalse(parent._shared)
        with self.assertRaises(ValueError):restore_run({'version':999})
        parent.finish()
        with self.assertRaises(backprop.Error):snapshot_run(parent)

    def test_fork_does_not_inherit_promoted_parent_writer_identity(self):
        parent=self.writer();parent.log({'parent':1});snapshot_run(parent);parent.finish()
        fork=self.writer(id='fork',settings={**self.settings,'fork_from':'same?_step=0'},config=None)
        self.assertFalse(fork._shared);self.assertNotEqual(fork._writer_id,parent._writer_id)
        self.assertEqual(fork.step,1);fork.finish()

    def test_disabled_transfer_writes_no_journal_and_is_pickleable(self):
        parent=self.writer(mode='disabled',_sink=None)
        descriptor=pickle.loads(pickle.dumps(snapshot_run(parent)))
        child=restore_run(descriptor);child.log({'ignored':1});child.finish()
        self.assertTrue(child.disabled);self.assertFalse(Path(child.sync_dir).exists())


if __name__=='__main__':unittest.main()
