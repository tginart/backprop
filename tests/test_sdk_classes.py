"""Class-label contracts and immutable persistence through existing storage.

Architecture: compare real pinned/native APIs in isolated processes, then send
mutable labels through Run and Artifact to verify snapshots and restart replay.
Main caller: the SDK suite; no environment installation or W&B service required.
"""
import contextlib
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from backprop.data_types import Encoder
from backprop.transport import read_journal, BackpropError

ROOT=Path(__file__).resolve().parents[1]


class ClassesContracts(unittest.TestCase):
    def tearDown(self):w.teardown()

    def test_pinned_constructor_aliases_signatures_binding_and_serialization(self):
        try: reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError: self.skipTest('Pinned W&B reference unavailable')
        self.assertEqual(reference.version,'0.30.0')
        values=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/classes_reference_probe.py'),mode],
                capture_output=True,text=True,timeout=40,cwd=tempfile.gettempdir(),env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            if mode=='reference' and "No module named 'wandb'" in result.stderr:self.skipTest('Pinned W&B reference unavailable')
            self.assertEqual(result.returncode,0,result.stderr)
            values.append(json.loads(result.stdout))
        self.assertEqual(values[0],values[1])

    def test_encoder_freezes_labels_and_preserves_ordinary_json_sequences(self):
        labels=({'id':1,'name':'one','metadata':{'color':'red'}},)
        value=w.Classes(labels)
        encoded=Encoder(lambda *a,**k:{}).encode(value)
        labels[0]['name']='changed';labels[0]['metadata']['color']='blue'
        self.assertEqual(encoded,{'_type':'classes','class_set':[{'id':1,'name':'one','metadata':{'color':'red'}}]})
        self.assertEqual(value.to_json(None)['class_set'][0]['name'],'changed')

    def test_artifact_roundtrip_survives_mutation_retry_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            labels=[{'id':0,'name':'background'},{'id':1,'name':'foreground'}]
            classes=w.Classes(labels)
            artifact=w.Artifact('labels','dataset');entry=artifact.add(classes,'labels')
            stored=entry._read_verified_bytes();labels[1]['name']='edited'
            self.assertEqual(artifact.get('labels').to_json(None)['class_set'][1]['name'],'foreground')
            sink=w.MemorySink();settings={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True}
            run=w.init(id='classes-recovery',entity='e',project='classes',dir=directory,_sink=sink,settings=settings)
            run.log({'labels':classes});run.log_artifact(artifact,aliases=['best']);journal=run.sync_dir
            run.finish();labels[0]['name']='after-finish'
            history=next(e for e in read_journal(journal) if e['type']=='history')
            self.assertEqual(history['payload']['metrics']['labels']['class_set'][0]['name'],'background')
            api=w.Api._from_store(sink,entity='e',project='classes')
            restored=api.artifact('labels:best');self.assertEqual(restored.get_entry('labels.classes.json')._read_verified_bytes(),stored)
            self.assertEqual(restored.get('labels'),w.Classes([{'id':0,'name':'background'},{'id':1,'name':'foreground'}]))
            sink.events.reverse();sink.events.extend(list(sink.events));api.flush()
            self.assertEqual(api.artifact('labels:best').get('labels'),restored.get('labels'))
            resumed=w.init(id='classes-recovery',entity='e',project='classes',dir=directory,_sink=sink,settings=settings,resume='must')
            resumed.log({'labels':classes});resumed.finish()
            api.flush();rows=list(api.run('classes-recovery').scan_history())
            self.assertEqual([r['labels']['class_set'][0]['name'] for r in rows],['background','after-finish'])

    def test_lost_upload_ack_retries_frozen_labels_without_duplicate_rows(self):
        with contextlib.ExitStack() as stack:
            directory=stack.enter_context(tempfile.TemporaryDirectory())
            sink=w.MemorySink()
            run=w.Run(id='lost-ack',entity='e',project='classes',dir=directory,_sink=sink,_deployment={},
                settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600})
            stack.callback(run.finish)
            labels=[{'id':1,'name':'before'}];value=w.Classes(labels);run.log({'labels':value})
            original=sink.publish
            def lost_ack(events):
                original(events)
                raise OSError('lost acknowledgement')
            with patch.object(sink,'publish',side_effect=lost_ack):
                with self.assertRaisesRegex(BackpropError, "lost acknowledgement"):run.flush()
            labels[0]['name']='after';run.flush();run.finish()
            self.assertGreater(len(sink.events),len({e['event_id'] for e in sink.events}))
            api=w.Api._from_store(sink,entity='e',project='classes')
            rows=list(api.run('lost-ack').scan_history())
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['labels']['class_set'],[{'id':1,'name':'before'}])

    def test_artifact_class_schema_validates_ids_and_restores_table(self):
        labels=w.Classes([{'id':0,'name':'zero'},{'id':2,'name':'two'}])
        table=w.Table(columns=['label'],data=[[0],[2]],dtype=labels.get_type(),optional=False)
        with self.assertRaises(TypeError):table.add_data(1)
        artifact=w.Artifact('typed-labels','dataset');artifact.add(table,'table')
        restored=artifact.get('table');self.assertEqual(restored.data,[[0],[2]])
        restored.add_data(0)
        with self.assertRaises(TypeError):restored.add_data(1)


if __name__=='__main__':unittest.main()
