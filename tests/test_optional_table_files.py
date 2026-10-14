"""Public Table file contracts and durable, self-contained artifact joins.

Architecture: independent pinned processes -> byte/signature comparison -> real
journals and immutable artifact reads -> retry, restart and delayed visibility.
Main callers: local SDK and optional CI. No GPU, network or new dependencies.
"""
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
from backprop.transport import BackpropError,sync
from backprop.apis.public.replay import reduce_events
from table_join_replay_fixture import SETTINGS,add_join,fixture
ROOT=Path(__file__).resolve().parents[1]


class TableFileContracts(unittest.TestCase):
    def test_public_bindings_and_join_inputs_match_pinned_at_each_import(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/table_file_reference_probe.py'),mode],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=60,
                env={**os.environ,'WANDB_DATA_DIR':str(Path(tempfile.gettempdir())/'backprop-reference-data'),'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observations.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'table-files-{mode}.json').write_text(result.stdout)
        self.assertEqual(observations[0].pop('source_sha256'),'652cc8b4a9d51ef78917a5ea213ac5ccb9af4fce704a2293c812a01d9aaa96bd')
        self.assertEqual(*observations)

    def test_binding_files_survive_exit_and_repeated_bound_files_remain_distinct(self):
        script="""
import json,sys
from pathlib import Path
import backprop as w
with w.Run(id='table-producer',project='table-file',entity='e',dir=sys.argv[1],mode='offline',settings={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True}) as run:
    table=w.Table(columns=['x'],data=[[1]],log_mode='INCREMENTAL')
    table.bind_to_run(run,'nested/value',0,id_=0,ignore_copy_err=False)
    first=Path(table._path).read_bytes()
    run.log({'table':table})
    table.add_data(2);table.bind_to_run(run,'nested/value',1,id_='next',ignore_copy_err=True)
    second=Path(table._path).read_bytes()
    run.log({'table':table})
    print(json.dumps({'directory':run.sync_dir,'first':first.decode(),'second':second.decode()}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,'-B','-c',script,directory],capture_output=True,text=True,cwd=tempfile.gettempdir(),env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')},timeout=30)
            self.assertEqual(result.returncode,0,result.stderr);record=json.loads(result.stdout);sink=w.MemorySink();sync(record['directory'],sink=sink)
            self.assertEqual(json.loads(record['first'])['data'],[[1]]);self.assertEqual(json.loads(record['second'])['data'],[[2]])
            api=w.Api._from_store(sink,entity='e',project='table-file');run=api.run('table-producer')
            files={file.name:file for file in run.files()}
            self.assertIn('media/table/nested/value_0_0.table.json',files)
            self.assertIn('media/table/nested/value_1_next.table.json',files)
            for name,key in [('media/table/nested/value_0_0.table.json','first'),('media/table/nested/value_1_next.table.json','second')]:
                file=files[name].download(root=str(Path(directory,'readback')),replace=True);self.assertEqual(Path(file.name).read_text(),record[key]);file.close()
            self.assertEqual(len(list(run.scan_history())),2)

    def test_cross_artifact_copy_is_self_contained_after_retry_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink()
            with w.Run(id='joined',entity='e',project='joins',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
                target,direct=add_join(run)
                run.log({'extra':1});original=sink.publish
                def lose_ack(events):original(events);raise OSError('lost acknowledgement')
                with patch.object(sink,'publish',side_effect=lose_ack):
                    with self.assertRaises(BackpropError):run.flush()
                run.flush()
            self.assertEqual(reduce_events(sink.events),reduce_events(list(reversed(sink.events))+sink.events))
            # Remove source artifact events entirely. Destination manifests/files
            # alone must resolve their copied table cells and schemas.
            def artifact_id(event):return event.get('payload',{}).get('artifact_id')
            sink.events[:]=[e for e in sink.events if e['type']!='artifact' or artifact_id(e)==target.id]
            sink.events[:]=list(reversed(sink.events))+sink.events
            api=w.Api._from_store(sink,entity='e',project='joins');saved=api.artifact(target.id);restored=saved.get('joined').materialize()
            self.assertEqual(restored.columns,direct.columns);self.assertEqual(len(restored.data),len(direct.data))
            for a,b in zip(restored.data,direct.data):
                self.assertEqual([v._bytes() if v is not None else None for v in [a[1],a[3]]],[v._bytes() if v is not None else None for v in [b[1],b[3]]])
            with w.Run(id='joined',entity='e',project='joins',dir=directory,_sink=sink,_deployment={},settings=SETTINGS,resume='must') as resumed:
                resumed.log({'joined':saved.get('joined')})
            self.assertEqual(len(list(api.run('joined').scan_history())),3)

    def test_failed_dependency_copy_rolls_back_and_retries_without_source_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink()
            with w.Run(id='copy-failure',project='joins',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
                source=w.Artifact('source','dataset');source.add(w.Table(columns=['key','value'],data=[[1,w.Html('<p>kept</p>',inject=False)]]),'input');run.log_artifact(source).wait()
                reader=w.Api._from_store(sink,project='joins').artifact(source.id)
                entry=reader.get_entry('input.table.json');target=w.Artifact('target','dataset');target.add(w.Table(columns=['x'],data=[[99]]),'unrelated');before=dict(target.manifest.entries)
                joined=w.JoinedTable(entry,entry,'key');download=sink.download_file
                with patch.object(sink,'download_file',side_effect=OSError('media not visible')):
                    with self.assertRaises(OSError):target.add(joined,'join')
                self.assertEqual(target.manifest.entries,before)
                target.add(joined,'join');restored=target.get('join').materialize()
                self.assertEqual(restored.data[0][1]._bytes(),b'<p>kept</p>');self.assertEqual(restored.data[0][3]._bytes(),b'<p>kept</p>')
                self.assertEqual(target.get('unrelated').data,[[99]])

    def test_shared_fixture_contains_duplicate_matches_and_unmatched_rows(self):
        result=fixture();self.assertTrue(any(a is None or b is None for a,b in result['expected']))
        duplicate=[pair for pair in result['expected'] if pair[0] in ['<p>left11</p>','<p>left12</p>']]
        self.assertEqual(len(duplicate),4)

if __name__=='__main__':unittest.main()
