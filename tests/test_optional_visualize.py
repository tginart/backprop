"""Verify public chart values and their durable native rendering snapshots.

Architecture: isolated pinned/native values -> real Run journals -> scoped chart
lookup -> retry/restart/shared reconstruction. Main callers: local SDK regression
and optional framework CI. These checks need no new service or environment.
"""
import copy
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
from backprop.legacy_charts import Visualize, VisualizeSpec

ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}


class LegacyVisualizationContracts(unittest.TestCase):
    def test_pinned_dataclass_constructors_properties_mutations_and_imports(self):
        try: reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError: self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');values=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/visualize_reference_probe.py'),mode],
                cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=60,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);values.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                output=ROOT/'test-results';output.mkdir(exist_ok=True)
                (output/f'visualize-{mode}.json').write_text(result.stdout)
        self.assertEqual(values[0],values[1])
        self.assertEqual(len(values[0]['spec']['cases']),12)
        self.assertEqual(sum(len(v['cases']) for v in values[0]['charts'].values()),40)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        environment=patch.dict(os.environ, {"BACKPROP_DIR":self.temp.name});environment.start();self.addCleanup(environment.stop)
        self.sink=w.MemorySink();self.api=w.Api._from_store(self.sink,entity='e',project='visualize')

    def writer(self,identity='chart',sink=None,settings=None,**kwargs):
        run=w.Run(id=identity,entity='e',project='visualize',dir=self.temp.name,_sink=sink or self.sink,
            _deployment={},settings={**SETTINGS,**(settings or {})},**kwargs)
        self.addCleanup(lambda:run.finish() if not run._closed else None)
        return run

    def test_real_tables_and_mutated_specs_keep_each_logged_snapshot(self):
        table=w.Table(columns=['feature_names','importances'],data=[['x',1]],log_mode='MUTABLE')
        spec=VisualizeSpec('wandb/feature_importances/v1');value=w.plot.Visualize(table=table,spec=spec)
        self.assertIs(value.spec,spec);self.assertIs(value.table,table)
        run=self.writer();run.log({'nested':{'importance':value}})
        self.assertEqual(spec.key,'nested.importance')
        table.add_data('y',2);run.log({'importance':value})
        spec.name='wandb/confusion_matrix/v1'
        value.table=w.Table(columns=['Predicted','Actual','Count'],data=[['a','b',3]])
        run.log({'confusion':value});run.finish()
        rows=self.api.run('chart').history(pandas=False)
        # Public reads flatten nested scalar/chart keys; stored values remain typed.
        self.assertEqual(rows[0]['nested.importance']['table']['data'],[['x',1]])
        self.assertEqual(rows[1]['importance']['table']['data'],[['x',1],['y',2]])
        self.assertEqual(rows[2]['confusion']['kind'],'confusion_matrix')
        self.assertEqual(rows[2]['confusion']['fields'],{'x':'Predicted','y':'Actual','value':'Count'})
        self.assertEqual(vars(spec),{'name':'wandb/confusion_matrix/v1','key':'confusion'})
        artifacts=list(self.api.run('chart').logged_artifacts())
        self.assertEqual(len(artifacts),3)
        lengths=[]
        for artifact in artifacts:
            names=[name for name in artifact._entries if name.endswith('.table.json')]
            self.assertEqual(len(names),1)
            lengths.append(len(artifact.get(names[0]).data))
        self.assertEqual(sorted(lengths),[1,1,2])

    def test_registered_definition_uses_receiving_store_and_refreshes_after_edit(self):
        def register(api,mark):
            return api.create_custom_chart('e','owned','Owned','vega2','private',{'data':{'name':'wandb'},'mark':mark,
                'encoding':{'x':{'field':'x','type':'quantitative'},'y':{'field':'y','type':'quantitative'}}})
        other=w.MemorySink();other_api=w.Api._from_store(other,entity='e',project='visualize')
        identity=register(self.api,'line');register(other_api,'point')
        value=Visualize(w.Table(columns=['x','y'],data=[[0,1]]),VisualizeSpec(identity))
        first=self.writer('first');second=self.writer('second',sink=other)
        with patch.object(w,'_active',side_effect=AssertionError('No global run lookup')):
            first.log({'chart':value});second.log({'chart':value})
            register(self.api,'bar');first.log({'chart':value})
        first.finish();second.finish()
        rows=self.api.run('first').history(pandas=False)
        self.assertEqual([row['chart']['spec']['mark'] for row in rows],['line','bar'])
        self.assertEqual(other_api.run('second').history(pandas=False)[0]['chart']['spec']['mark'],'point')
        self.assertEqual(set(vars(value.spec)),{'name','key'})

    def test_failed_lookup_and_invalid_row_leave_keys_and_table_available_for_retry(self):
        value=Visualize(w.Table(columns=['x'],data=[[1]],log_mode='INCREMENTAL'),VisualizeSpec('e/missing','before'))
        run=self.writer()
        with self.assertRaisesRegex(ValueError,'Unknown Backprop chart'):run.log({'chart':value})
        self.assertEqual(value.spec.key,'before');self.assertEqual(value.table._offset,0)
        self.api.create_custom_chart('e','missing','Late','vega2','private',{'data':{'name':'wandb'},'mark':'point','encoding':{'x':{'field':'x','type':'quantitative'}}})
        with self.assertRaises(TypeError):run.log({'chart':value,'invalid':object()})
        self.assertEqual(value.spec.key,'before');self.assertEqual(value.table._offset,0)
        run.log({'chart':value});run.finish()
        self.assertEqual(value.table._offset,1);self.assertEqual(value.spec.key,'chart')
        self.assertEqual(self.api.run('chart').history(pandas=False)[0]['chart']['table']['data'],[[1]])

    def test_independent_process_crash_keeps_chart_and_table_snapshots_recoverable(self):
        script = """
import os,sys,json
from pathlib import Path
import backprop as w
from backprop.legacy_charts import VisualizeSpec
run=w.Run(id='crashed',entity='e',project='visualize',mode='offline',dir=sys.argv[1],_deployment={},settings=json.loads(sys.argv[2]))
table=w.Table(columns=['feature_names','importances'],data=[['first',1]],log_mode='MUTABLE')
chart=w.plot.Visualize(table=table,spec=VisualizeSpec('wandb/feature_importances/v1'))
run.log({'chart':chart});table.add_data('second',2);run.log({'chart':chart})
print(str(run._journal.directory),flush=True)
os._exit(0)
"""
        result=subprocess.run([sys.executable,'-B','-c',script,self.temp.name,json.dumps(SETTINGS)],
            cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=30,
            env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
        self.assertEqual(result.returncode,0,result.stderr)
        journal=Path(result.stdout.strip())
        count=w.sync(journal,sink=self.sink);self.assertGreater(count,0)
        self.assertEqual(w.sync(journal,sink=self.sink),count)
        rows=self.api.run('crashed').history(pandas=False)
        self.assertEqual([row['chart']['table']['data'] for row in rows],[[['first',1]],[['first',1],['second',2]]])
        self.assertEqual(len({event['event_id'] for event in self.sink.events}),len(self.sink.events))

    def test_lost_ack_duplicate_delay_resume_and_shared_writers(self):
        run=self.writer()
        value=Visualize(w.Table(columns=['feature_names','importances'],data=[['x',1]]),VisualizeSpec('wandb/feature_importances/v1'))
        run.log({'chart':value});run.flush();earlier=copy.deepcopy(self.sink.events)
        value.table=w.Table(columns=['feature_names','importances'],data=[['y',2]])
        run.log({'chart':value});publish=self.sink.publish
        def lost_ack(events):publish(events);raise OSError('lost acknowledgement')
        with patch.object(self.sink,'publish',side_effect=lost_ack):
            with self.assertRaises(w.Error):run.flush()
        run.flush();run.finish()
        resumed=self.writer(resume='must');resumed.log({'chart':value});resumed.finish()
        self.sink.events=self.sink.events[::-1]+copy.deepcopy(self.sink.events)
        reader=self.api.run('chart')
        with patch.object(self.sink,'read_events',return_value=earlier):
            self.assertEqual(len(reader.history(pandas=False)),1)
        self.assertEqual(len(reader.history(pandas=False)),3)
        primary=self.writer('shared',mode='shared',settings={'x_primary':True,'x_label':'rank0'})
        secondary=self.writer('shared',mode='shared',settings={'x_primary':False,'x_label':'rank1'})
        primary.log({'chart':value});secondary.log({'chart':value});primary.finish();secondary.finish()
        rows=self.api.run('shared').history(pandas=False)
        self.assertEqual(len(rows),2);self.assertEqual(len({row['_writer'] for row in rows}),2)
        self.assertEqual([row['_step'] for row in rows],[0,0])


if __name__=='__main__':unittest.main()
