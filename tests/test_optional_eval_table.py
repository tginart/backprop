"""Evaluation lifecycle at the reference, journal and public-read boundaries.

Architecture: pinned isolated observations -> native durable failure/retry ->
independent writer restart -> immutable artifact readback. Main callers: local
SDK checks and optional framework CI. No extra service, dependency or resources.
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
from backprop.transport import read_journal, sync
from backprop.artifact_events import iter_artifact_events
ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True,'flush_interval':600}


class EvalTableContracts(unittest.TestCase):
    def test_isolated_pinned_signatures_roles_values_errors_and_lifecycle(self):
        try: reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        try:import numpy; import PIL
        except ImportError:self.skipTest('Optional NumPy/Pillow required for reference media cases')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/eval_table_reference_probe.py'),mode],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=60,
                env={**os.environ,'WANDB_DATA_DIR':str(Path(tempfile.gettempdir())/'backprop-reference-data'),'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observations.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'eval-table-{mode}.json').write_text(result.stdout)
        self.assertEqual(*observations)

    def test_failed_and_overwritten_history_does_not_freeze_or_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            run=w.Run(id='eval-atomic',entity='e',project='eval',dir=directory,mode='offline',settings=SETTINGS)
            table=w.EvalTable(columns=['answer','score'],score_columns=['score'],data=[['a',1]])
            with patch.object(run._journal,'append',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):run.log({'evaluation':table})
            self.assertFalse(table.has_been_logged());self.assertFalse(table.is_bound());self.assertEqual(table._offset,0)
            self.assertEqual(list(iter_artifact_events(read_journal(run.sync_dir))),[])
            run.log({'evaluation':table});first=dict(run.summary['evaluation'])
            self.assertEqual(table._run_log_key,'evaluation');self.assertTrue(table.has_been_logged());self.assertFalse(table.file_is_set())
            table.add_data('b',3);table.add_column('extra',[1,2]);run.log({'again':table})
            self.assertEqual(run.summary['again'],first)
            self.assertEqual(table.evaluation_metadata()['evaluation_scores']['score'],{'mean':1.,'count':1})
            self.assertEqual(table.evaluation_roles['output_columns'],['answer'])
            discarded=w.EvalTable(columns=['x'],data=[[1]])
            run.log({'discarded':discarded},commit=False);run.log({'discarded':42})
            self.assertFalse(discarded.has_been_logged());self.assertFalse(discarded.is_bound())
            self.assertEqual(len(list(iter_artifact_events(read_journal(run.sync_dir)))),1)
            run.finish()

    def test_explicit_serialization_failure_retries_without_history_step(self):
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink();run=w.Run(id='eval-explicit',entity='e',project='eval',dir=directory,_sink=sink,settings=SETTINGS)
            table=w.EvalTable(columns=['value'],data=[[w.Html('<p>unsupported</p>')]])
            table.bind_to_run(run,key=12,step=3,id_='ignored',ignore_copy_err=True)
            with patch.object(run._journal,'append',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):table.to_json(run)
            self.assertTrue(table.is_bound());self.assertFalse(table.has_been_logged());self.assertFalse(table.file_is_set())
            descriptor=table.to_json(run);self.assertIsInstance(descriptor['evaluate_call_id'],str)
            self.assertEqual(descriptor,table.to_json(run));self.assertEqual(run.step,0)
            run.finish();api=w.Api._from_store(sink,entity='e',project='eval')
            artifact=api.artifact(descriptor['evaluate_call_id']);self.assertEqual(artifact.get('12').data,[['[wandb.Html not yet supported]']])
            self.assertEqual(len(list(api.run(run.id).scan_history())),0)

    def test_pending_alias_and_invalid_container_do_not_leak_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            run=w.Run(id='eval-pending',entity='e',project='eval',dir=directory,mode='offline',settings=SETTINGS)
            table=w.EvalTable(columns=['x'],data=[[1]])
            run.log({'original':table},commit=False);run.log({'alias':table},commit=False);run.log({'original':5})
            self.assertTrue(table.has_been_logged());self.assertEqual(run.summary['alias']['data'],[[1]])
            self.assertEqual(len(list(iter_artifact_events(read_journal(run.sync_dir)))),1)
            bad=w.EvalTable(columns=['x'],data=[[{'nested':[w.Table(columns=['a'])]}]],allow_mixed_types=True)
            with self.assertRaises(TypeError):run.log({'bad':bad})
            self.assertFalse(bad.has_been_logged());self.assertFalse(bad.is_bound());run.log({'next':1});run.finish()

    def test_independent_shared_writers_restart_duplicate_and_delayed_files(self):
        script = """
import json,sys
import backprop as w
class Disconnected(w.MemorySink):
    disconnected=False
    def publish(self,events):
        if self.disconnected:raise ConnectionError('writer disconnected')
        return super().publish(events)
settings=json.loads(sys.argv[3]);settings.update(x_primary=sys.argv[2]=='primary',x_label=sys.argv[2])
sink=Disconnected()
run=w.Run(id='shared-eval',entity='e',project='eval',dir=sys.argv[1],mode='shared',_sink=sink,settings=settings)
sink.disconnected=True
table=w.EvalTable(columns=['answer','score'],score_columns=['score'],data=[[sys.argv[2],1]])
run.log({sys.argv[2]:table},step=0,commit=True)
identity=table.to_json(run)['evaluate_call_id']
try:run.finish()
except w.Error as error:assert isinstance(error.__cause__,ConnectionError)
else:raise AssertionError('disconnected writer unexpectedly uploaded')
print(json.dumps({'directory':run.sync_dir,'identity':identity,'initial_events':sink.events}))
"""
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink();records=[]
            for writer in ['secondary','primary']:
                result=subprocess.run([sys.executable,'-B','-c',script,directory,writer,json.dumps(SETTINGS)],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
                self.assertEqual(result.returncode,0,result.stderr);record=json.loads(result.stdout);records.append(record);sink.publish(record['initial_events'])
                with patch.object(sink,'publish',side_effect=ConnectionError('retry')):
                    with self.assertRaises(w.Error):sync(record['directory'],sink=sink)
                sync(record['directory'],sink=sink);sync(record['directory'],sink=sink)
            sink.events[:]=list(reversed(sink.events))+list(sink.events)
            self.assertNotEqual(records[0]['identity'],records[1]['identity'])
            api=w.Api._from_store(sink,entity='e',project='eval');run=api.run('shared-eval')
            self.assertEqual(run.state,'finished');self.assertEqual(len(list(run.scan_history())),2)
            self.assertEqual(len(list(run.logged_artifacts())),2)
            for writer,record in zip(['secondary','primary'],records):
                self.assertEqual(run.summary[writer]['evaluate_call_id'],record['identity'])
                artifact=api.artifact(record['identity'])
                with patch.object(sink,'download_file',side_effect=FileNotFoundError('delayed')):
                    with self.assertRaises(FileNotFoundError):artifact.get(writer)
                saved=artifact.get(writer);self.assertEqual(saved.data,[[writer,1]])
                self.assertEqual(saved.evaluation_roles['score_columns'],['score'])

    def test_disabled_and_finished_publication_is_durable_without_reopening_run(self):
        from backprop.evaluation_publication import _writer_for
        for mode in ['disabled','finished','offline-finished']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                sink=w.MemorySink();run=w.Run(id='independent',entity='e',project='eval',dir=directory,
                    mode='disabled' if mode=='disabled' else 'offline' if mode=='offline-finished' else 'online',
                    _sink=None if mode=='offline-finished' else sink,settings=SETTINGS)
                if mode!='disabled':run.finish()
                original_events=list(sink.events);step=run.step;closed=run._closed
                table=w.EvalTable(columns=['x'],data=[[1]]);table.bind_to_run(run,'independent',0)
                writer=_writer_for(run)
                with patch.object(writer._journal,'append',side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):table.to_json(run)
                self.assertFalse(table.has_been_logged());self.assertEqual(read_journal(writer._journal.directory),[])
                if mode!='offline-finished':
                    publish=sink.publish
                    def lose_ack(events):publish(events);raise ConnectionError('lost acknowledgement')
                    with patch.object(sink,'publish',side_effect=lose_ack):
                        with self.assertRaises(w.Error):table.to_json(run)
                    self.assertTrue(table.has_been_logged());table.add_data(2)
                descriptor=table.to_json(run);identity=descriptor['evaluate_call_id']
                self.assertEqual(descriptor,table.to_json(run));self.assertEqual(writer._journal.count,1)
                sync(writer._journal.directory,sink=sink);sync(writer._journal.directory,sink=sink)
                self.assertEqual(run._closed,closed);self.assertEqual(run.step,step)
                self.assertEqual([e for e in sink.events if e['type']!='artifact'],original_events)
                artifact=w.Api._from_store(sink,entity='e',project='eval').artifact(identity)
                self.assertEqual(artifact.get('independent').data,[[1]])
                if mode=='disabled':
                    self.assertIsNone(run._journal);run.log({'ignored':1});self.assertEqual(run.step,0)
                run.finish()

    def test_independent_journal_recovers_after_process_exit_and_late_files(self):
        script="""
import json,sys
import backprop as w
class Disconnected(w.MemorySink):
    def publish(self,events):raise ConnectionError('disconnected')
run=w.Run(id='independent',entity='e',project='eval',dir=sys.argv[1],mode='disabled',_sink=Disconnected(),settings=json.loads(sys.argv[2]))
table=w.EvalTable(columns=['x'],data=[[1]]);table.bind_to_run(run,'after-exit',0)
try:table.to_json(run)
except w.Error:pass
else:raise AssertionError('Expected failed upload')
print(json.dumps({'directory':str(run._evaluation_writer._journal.directory),'identity':table._immutable_evaluate_call_id}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,'-B','-c',script,directory,json.dumps(SETTINGS)],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);record=json.loads(result.stdout);sink=w.MemorySink()
            sync(record['directory'],sink=sink);sync(record['directory'],sink=sink)
            sink.events[:]=list(reversed(sink.events))+sink.events
            artifact=w.Api._from_store(sink,entity='e',project='eval').artifact(record['identity'])
            with patch.object(sink,'download_file',side_effect=FileNotFoundError('not visible yet')):
                with self.assertRaises(FileNotFoundError):artifact.get('after-exit')
            self.assertEqual(artifact.get('after-exit').data,[[1]])

    def test_disabled_explicit_publication_uses_foundry_credentials_only_on_demand(self):
        from backprop.login_session import AuthCandidate
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink();config={'host':'https://foundry.example','stream_dataset_rid':'existing'}
            with patch('backprop.login_session.authenticate_candidate',return_value=AuthCandidate('test-token',config['host'])) as authenticate,patch('backprop.transport.native_store',return_value=sink) as store:
                run=w.Run(id='disabled-auth',project='eval',dir=directory,mode='disabled',_deployment=config,settings=SETTINGS)
                run.log({'ignored':1});authenticate.assert_not_called();store.assert_not_called()
                table=w.EvalTable(columns=['x'],data=[[1]]);table.bind_to_run(run,'explicit',0);table.to_json(run)
                authenticate.assert_called_once();store.assert_called_once_with(run._deployment,token='test-token')
                self.assertEqual([e['type'] for e in sink.events],['artifact']);run.finish()

    def test_concurrent_explicit_calls_reuse_one_manifest(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink();run=w.Run(id='concurrent',project='eval',dir=directory,mode='disabled',_sink=sink,settings=SETTINGS)
            table=w.EvalTable(columns=['x'],data=[[1]]);table.bind_to_run(run,'concurrent',0)
            with ThreadPoolExecutor(max_workers=4) as executor:
                descriptors=list(executor.map(lambda _:table.to_json(run),range(12)))
            self.assertTrue(all(value==descriptors[0] for value in descriptors))
            self.assertEqual(len(sink.events),1);self.assertIsNone(run._thread);run.finish()
