"""Public Table JSON contracts at real file and durable journal boundaries.

Architecture: independent pinned processes -> public schemas and validation ->
real artifact snapshots -> restart/retry reads with immutable bytes. Native
storage keeps its richer schema; explicit serializers use the public schema.
Main callers: local SDK suite and optional-framework CI, with no new installs.
"""
from datetime import datetime,timezone
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
from backprop.transport import sync
from backprop.object_codec import decode_object

ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'console':'off','silent':True,'disable_git':True,'x_disable_stats':True,'flush_interval':600}


class PublicTableSerialization(unittest.TestCase):
    def setUp(self):
        self.original_directory=Path.cwd();self.downloads=tempfile.TemporaryDirectory()
        os.chdir(self.downloads.name)

    def tearDown(self):
        os.chdir(self.original_directory);self.downloads.cleanup()

    def test_public_signatures_types_files_errors_and_aliases_match_pinned(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/table_serialization_reference_probe.py'),mode],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=60,
                env={**os.environ,'WANDB_DATA_DIR':str(Path(tempfile.gettempdir())/'backprop-reference-data'),'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observations.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'table-serialization-{mode}.json').write_text(result.stdout)
        self.assertEqual(observations[0].pop('source_sha256'),'652cc8b4a9d51ef78917a5ea213ac5ccb9af4fce704a2293c812a01d9aaa96bd')
        self.assertEqual(*observations)

    def test_typed_schema_file_survives_process_exit_retries_and_media_roundtrip(self):
        try:import numpy as np
        except ImportError:self.skipTest('NumPy unavailable')
        script='''
import json,sys
from datetime import datetime,timezone
import numpy as np
import backprop as w
settings=json.loads(sys.argv[2])
with w.Run(id='public-table',entity='e',project='public-tables',dir=sys.argv[1],mode='offline',settings=settings) as run:
    artifact=w.Artifact('typed-public','dataset')
    source=w.Table(columns=['time','array','html'],data=[[datetime(2024,1,1,tzinfo=timezone.utc),np.arange(6).reshape(2,3),w.Html('<p>saved</p>',inject=False)]])
    with artifact.new_file('public.table.json',mode='w') as stream:json.dump(source.to_json(run_or_artifact=artifact),stream)
    artifact.add(source,'native');run.log_artifact(artifact)
    print(json.dumps({'directory':run.sync_dir,'artifact_id':artifact.id}))
'''
        with tempfile.TemporaryDirectory() as directory:
            result=subprocess.run([sys.executable,'-B','-c',script,directory,json.dumps(SETTINGS)],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);record=json.loads(result.stdout);sink=w.MemorySink()
            with patch.object(sink,'publish',side_effect=ConnectionError('retry')):
                with self.assertRaises(w.Error):sync(record['directory'],sink=sink)
            sync(record['directory'],sink=sink);sync(record['directory'],sink=sink)
            sink.events[:]=list(reversed(sink.events))+list(sink.events)
            artifact=w.Api._from_store(sink,entity='e',project='public-tables').artifact('typed-public:latest')
            # Failed visibility cannot cache an incomplete decode.
            with patch.object(sink,'download_file',side_effect=FileNotFoundError('delayed')):
                with self.assertRaises(FileNotFoundError):artifact.get('public')
            table=artifact.get('public');native=artifact.get('native')
            self.assertEqual(table.data[0][0],datetime(2024,1,1,tzinfo=timezone.utc))
            np.testing.assert_array_equal(table.data[0][1],np.arange(6).reshape(2,3))
            self.assertEqual(table.data[0][2]._bytes(),b'<p>saved</p>')
            self.assertEqual(table.data[0][0],native.data[0][0]);np.testing.assert_array_equal(table.data[0][1],native.data[0][1])
            self.assertEqual(table._column_types,native._column_types)

    def test_evaluation_roles_survive_both_explicit_and_native_formats(self):
        artifact=w.Artifact('eval-public','dataset')
        table=w.EvalTable(columns=['input','score'],data=[['a',0.9]],input_columns=['input'],score_columns=['score'])
        public=table.to_json(artifact)
        for result in [w.EvalTable.from_json(public,artifact),decode_object(public,artifact)]:
            self.assertIsInstance(result,w.EvalTable);self.assertEqual(result.data,table.data)
            self.assertEqual(result.evaluation_metadata(),table.evaluation_metadata())
        artifact.add(table,'native');self.assertEqual(artifact.get('native').evaluation_metadata(),table.evaluation_metadata())

    def test_array_failure_retry_preserves_rows_and_logging_cursor(self):
        try:import numpy as np
        except ImportError:self.skipTest('NumPy unavailable')
        artifact=w.Artifact('retry-array','dataset');table=w.Table(columns=['x'],data=[[np.arange(4).reshape(2,2)]])
        before=(table._generation,table._offset,table._snapshot)
        schema_before=json.dumps(table._column_types.to_json(artifact))
        with patch.object(artifact,'add_file',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):table.to_json(artifact)
        self.assertEqual((table._generation,table._offset,table._snapshot),before)
        self.assertEqual(json.dumps(table._column_types.to_json(artifact)),schema_before)
        descriptor=table.to_json(artifact);restored=w.Table.from_json(descriptor,artifact)
        np.testing.assert_array_equal(restored.data[0][0],table.data[0][0])
        self.assertEqual((table._generation,table._offset,table._snapshot),before)
        path=next(item for item in descriptor['column_types']['params']['type_map']['x']['params']['allowed_types'] if item['wb_type']=='ndarray')['params']['serialization_path']['path']
        with patch.object(artifact,'get_entry',side_effect=FileNotFoundError(path)):
            with self.assertRaises(FileNotFoundError):w.Table.from_json(descriptor,artifact)
        np.testing.assert_array_equal(w.Table.from_json(descriptor,artifact).data[0][0],table.data[0][0])

    def test_increment_metadata_changes_only_after_durable_log(self):
        with tempfile.TemporaryDirectory() as directory:
            run=w.Run(id='cursor',entity='e',project='public-tables',dir=directory,mode='offline',settings=SETTINGS)
            table=w.Table(columns=['x'],log_mode='INCREMENTAL')
            for index in range(2):
                table.add_data(index);run.log({'table':table})
                descriptor=table.to_json(w.Artifact('cursor-view','dataset'))
                self.assertEqual(descriptor['increment_num'],index)
                self.assertEqual(len(descriptor['previous_increments_paths']),index)
                self.assertEqual(descriptor['data'],[]);self.assertEqual(table._offset,index+1)
            before=(table._increment_num,list(table._previous_increments_paths),table._offset)
            table.add_data(2)
            with patch.object(run._journal,'append',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):run.log({'table':table})
            self.assertEqual((table._increment_num,table._previous_increments_paths,table._offset),before)
            run.log({'table':table});self.assertEqual(table._increment_num,2);run.finish()
