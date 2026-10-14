"""Constructor parity through genuine SDKs and durable table/evaluation reads.

Architecture: isolated pinned/native observations -> sparse schema repair ->
publication failure/retry -> restart replay of NumPy/DataFrame cell types.
Main callers: SDK and optional-framework CI. No local dependency installation.
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
from backprop.transport import read_journal,sync
from backprop.artifact_events import iter_artifact_events
ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True,'flush_interval':600}


class TableConstructorContracts(unittest.TestCase):
    def test_complete_input_selection_and_aliases_match_pinned(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/table_constructor_reference_probe.py'),mode],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=60,
                env={**os.environ,'WANDB_DATA_DIR':str(Path(tempfile.gettempdir())/'backprop-reference-data'),'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observations.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'table-constructor-{mode}.json').write_text(result.stdout)
        self.assertEqual(*observations)

    def test_missing_declarations_require_repair_before_native_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            run=w.Run(id='sparse',entity='e',project='constructors',dir=directory,mode='offline',settings=SETTINGS)
            table=w.Table(columns=['a','b'],dtype=[int]);before=table._column_types.to_json()
            self.assertEqual(set(before['params']['type_map']),{'a'})
            with self.assertRaises(TypeError):table.add_data(1,2)
            with self.assertRaises(KeyError):run.log({'table':table})
            self.assertEqual(table._column_types.to_json(),before);self.assertFalse(table.has_been_logged())
            self.assertEqual(list(iter_artifact_events(read_journal(run.sync_dir))),[])
            artifact=w.Artifact('sparse','dataset')
            with self.assertRaises(KeyError):artifact.add(table,'table')
            self.assertEqual(artifact.manifest.entries,{})
            table.cast('b',int);table.add_data(1,2)
            with patch.object(run._journal,'append',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):run.log({'table':table})
            self.assertFalse(table.has_been_logged());run.log({'table':table});run.finish()
            sink=w.MemorySink();sync(run.sync_dir,sink=sink);sync(run.sync_dir,sink=sink)
            artifact=next(iter(w.Api._from_store(sink,entity='e',project='constructors').run('sparse').logged_artifacts()))
            restored=artifact.get('table');self.assertEqual(restored.data,[[1,2]])
            restored.add_data(3,4)
            with self.assertRaises(TypeError):restored.add_data(5,'wrong')

    def test_column_alias_and_failed_computation_keep_original_list_consistent(self):
        columns=['x'];table=w.Table(columns=columns,data=[[1],[2]])
        self.assertIs(table.columns,columns)
        with self.assertRaises(TypeError):table.add_computed_columns(lambda index,row:{'a':index,'b':1 if index==0 else 'bad'})
        self.assertEqual(columns,['x']);self.assertEqual(table.data,[[1],[2]])
        table.add_computed_columns(lambda index,row:{'a':index});self.assertEqual(columns,['x','a'])
        self.assertIs(table.columns,columns)
        self.assertEqual(set(table._column_types.params['type_map']),set(columns))

    def test_empty_eval_can_publish_without_type_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            run=w.Run(id='empty',entity='e',project='constructors',dir=directory,mode='offline',settings=SETTINGS)
            table=w.EvalTable(columns=['x'],dtype=[]);run.log({'evaluation':table})
            self.assertTrue(table.has_been_logged());self.assertEqual(run.summary['evaluation']['data'],[])
            self.assertEqual(run.summary['evaluation']['evaluation_roles']['output_columns'],['x'])
            self.assertEqual(table._column_types.params['type_map'],{})
            with self.assertRaises(TypeError):table.add_data(1)
            run.finish()

    def test_numpy_dataframe_cells_survive_exit_upload_retry_and_artifact_read(self):
        try:import numpy as np;import pandas as pd
        except ImportError:self.skipTest('NumPy and pandas required')
        script="""
import json,sys,numpy as np,pandas as pd
import backprop as w
with w.Run(id='raw-cells',entity='e',project='constructors',dir=sys.argv[1],mode='offline',settings=json.loads(sys.argv[2])) as run:
    frame=pd.DataFrame({'i':np.array([1,2],dtype='i2'),'f':np.array([1.5,2.5],dtype='f4')})
    tensor=np.arange(8,dtype='i2').reshape(2,1,2,2)
    table=w.Table(dataframe=frame,optional=False);array=w.Table(columns=['array'],data=tensor,optional=False)
    run.log({'frame':table,'array':array})
    print(json.dumps({'directory':run.sync_dir}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,'-B','-c',script,directory,json.dumps(SETTINGS)],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);record=json.loads(result.stdout);sink=w.MemorySink()
            with patch.object(sink,'publish',side_effect=ConnectionError('retry')):
                with self.assertRaises(w.Error):sync(record['directory'],sink=sink)
            sync(record['directory'],sink=sink);sync(record['directory'],sink=sink)
            sink.events[:]=list(reversed(sink.events))+list(sink.events)
            api=w.Api._from_store(sink,entity='e',project='constructors');saved={a.name.split(':')[0]:a for a in api.run('raw-cells').logged_artifacts()}
            artifact=saved['run-raw-cells-array']
            with patch.object(sink,'download_file',side_effect=FileNotFoundError('delayed')):
                with self.assertRaises(FileNotFoundError):artifact.get('array')
            table=artifact.get('array');np.testing.assert_array_equal(table.data[0][0],np.arange(4,dtype='i2').reshape(2,2))
            self.assertEqual(str(table.data[0][0].dtype),'int16');table.add_data(np.ones((2,2),dtype='i2'))
            with self.assertRaises(TypeError):table.add_data(np.ones((2,3)))
            self.assertEqual(saved['run-raw-cells-frame'].get('frame').data,[[1,1.5],[2,2.5]])
