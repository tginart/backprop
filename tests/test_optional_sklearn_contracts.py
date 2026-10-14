"""Real sklearn values, repaired valid inputs and durable chart publication.

Architecture: isolated reference/native observations -> explicit repair oracles
-> native run journals -> independent readers and browser replay. Main callers:
local SDK validation and both existing sklearn version-boundary CI jobs.
"""
import copy
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
AVAILABLE=all(importlib.util.find_spec(name) for name in ['sklearn','numpy','pandas','scipy'])

def reference_with_correct_axes(value):
    """Account only for the documented pinned confusion-matrix axis defect.

    Main caller: the paired comparison. Raw observations remain in CI artifacts.
    Counts and order are unchanged; two cell labels are exchanged because W&B
    labels its true-label row axis as Predicted. Filtering is tested separately.
    """
    if isinstance(value,list):return [reference_with_correct_axes(v) for v in value]
    if isinstance(value,dict):
        value={k:reference_with_correct_axes(v) for k,v in value.items()}
        if value.get('spec',{}).get('name')=='wandb/confusion_matrix/v1':
            value['table']['data']=[[actual,predicted,count] for predicted,actual,count in value['table']['data']]
    return value

@unittest.skipUnless(AVAILABLE,'optional sklearn numerical dependencies are unavailable')
class SklearnContracts(unittest.TestCase):
    def test_pinned_signatures_values_errors_mutations_and_both_aliases(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');observed=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/sklearn_reference_probe.py'),mode],
                cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=180,
                env={**os.environ,'PYTHONHASHSEED':'0','OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1',
                    'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);observed.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                folder=ROOT/'test-results';folder.mkdir(exist_ok=True)
                (folder/f'sklearn-{mode}.json').write_text(result.stdout)
        self.assertEqual(reference_with_correct_axes(observed[0]['portable']),observed[1]['portable'])
        self.assertEqual(len(observed[0]['portable']),2)
        for prefix,group in observed[1]['portable'].items():
            self.assertEqual(len(group),8)
            controls=observed[1]['controls'][prefix]
            self.assertEqual(len(controls),7)
            for name,contract in controls.items():
                original=observed[0]['controls'][prefix][name]
                self.assertEqual(original['signature'],contract['signature'])
                for label in ['defaults','unknown_argument']:
                    self.assertEqual(original['cases'][label],contract['cases'][label])
            # Compare the unchanged portions of repaired helpers, retaining
            # raw observations. Only declared controls are adjusted here; their
            # actual values have separate numerical and browser oracles.
            for name,contract in controls.items():
                original=observed[0]['controls'][prefix][name]
                for label,actual in contract['cases'].items():
                    if label in ['defaults','unknown_argument']:continue
                    expected=copy.deepcopy(reference_with_correct_axes(original['cases'][label]))
                    actual=copy.deepcopy(actual)
                    if name in ['plot_roc','plot_precision_recall'] and label=='empty_classes':
                        self.assertEqual(expected['result']['error'],'ValueError')
                        self.assertEqual(actual['result'],{'value':None})
                        continue  # New aggregate-only behavior has its own oracle.
                    if name=='plot_classifier' and label in ['binary','multiclass']:
                        expected['calls']=[call for call in expected['calls'] if not (
                            call[0]=='log' and 'calibration_curve' in call[1][0]
                            or call[0]=='termlog' and call[1]==['Logged calibration curve.']
                            or call[0]=='termwarn' and 'calibration' in str(call[1]).lower())]
                    def adapt(value,native):
                        if isinstance(value,list):return [adapt(item,native) for item in value]
                        if not isinstance(value,dict):return value
                        value={key:adapt(item,native) for key,item in value.items() if key!='render_options'}
                        if 'table' not in value or 'spec' not in value:return value
                        table=value['table'];identity=value['spec'].get('name')
                        if native and value['spec'].get('spec_name')=='wandb/area-under-curve/v0':
                            table['data']=[row for row in table['data'] if row[0] not in ['micro-average','macro-average']]
                        if not native and identity=='wandb/feature_importances/v1' and name=='plot_feature_importances':
                            limit=2 if label=='all_options' else 0 if label=='zero_limit' else 50
                            table['data']=table['data'][:limit]
                        if not native and identity=='wandb/silhouette_/v1' and (
                                name=='plot_clusterer' or name=='plot_silhouette' and label=='options'):
                            for row in table['data']:
                                row[2]=['A','B','C'][int(row[2])];row[8]=['A','B','C'][int(row[8])]
                        return value
                    self.assertEqual(adapt(expected,False),adapt(actual,True),(prefix,name,label))
            # Presence/equal early failure is insufficient: every helper must
            # have at least one successful case that produced a real chart.
            for name,contract in {**group,**controls}.items():
                self.assertTrue(any(v['result']=={'value':None} and any(c[0]=='log' for c in v['calls'])
                    for v in contract['cases'].values()),name)
            before=observed[0]['repairs'][prefix];after=observed[1]['repairs'][prefix]
            for name in ['plot_summary_metrics','plot_feature_importances','plot_calibration_curve']:
                key=name+'.three_feature_repair'
                self.assertFalse(any(v[0]=='log' for v in before[key]['calls']))
                self.assertTrue(any(v[0]=='log' for v in after[key]['calls']))
            for name in ['plot_silhouette','plot_clusterer']:
                key=name+'.modern_tags_repair'
                self.assertEqual(after[key]['result'],{'value':None})
                self.assertTrue(any(v[0]=='log' and 'silhouette_plot' in v[1][0] for v in after[key]['calls']))
            for label in ['true_subset','pred_subset','both_subsets','named_subset','invalid_subset']:
                self.assertEqual(before['plot_confusion_matrix.'+label]['result']['error'],'AssertionError')
            self.assertEqual(after['plot_confusion_matrix.invalid_subset']['result']['error'],'ValueError')

    def test_confusion_labels_filters_counts_normalization_and_validation(self):
        import numpy as np
        import backprop as w
        from backprop.integration.sklearn import plot_confusion_matrix
        from sklearn.metrics import confusion_matrix
        from unittest.mock import patch
        truth=[0,0,1,1,2,2];predicted=[0,1,0,1,1,2]
        with patch.object(w,'log') as log:
            for labels in [None,['cat','dog','owl'],[10,20,30]]:
                display=[0,1,2] if labels is None else labels
                for normalize in [False,True]:
                    full=confusion_matrix(truth,predicted)
                    if normalize:full=np.around(full/full.sum(axis=1)[:,None],2)
                    for rows,columns in [(None,None),([display[0],display[2]],[display[1]]),([],[display[0]])]:
                        plot_confusion_matrix(truth,predicted,labels=labels,true_labels=rows,pred_labels=columns,normalize=normalize)
                        table=log.call_args.args[0]['confusion_matrix'].table
                        expected=[[display[j],display[i],full[i,j]] for i in range(3) for j in range(3)
                            if (rows is None or display[i] in rows) and (columns is None or display[j] in columns)]
                        self.assertEqual(table.data,expected)
            plot_confusion_matrix(truth,predicted,true_labels=iter([0,2]),pred_labels=(1,))
            self.assertEqual(log.call_args.args[0]['confusion_matrix'].table.data,[[1,0,1],[1,2,1]])
            for kwargs,error in [({'true_labels':[99]},ValueError),({'pred_labels':'cat'},TypeError),({'labels':['cat']},ValueError)]:
                with self.assertRaises(error):plot_confusion_matrix(truth,predicted,**kwargs)

    def test_every_helper_logs_real_charts_then_replay_retry_and_crash_recover(self):
        import backprop as w
        for crash in [False,True]:
            with tempfile.TemporaryDirectory() as directory:
                result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/sklearn_replay_fixture.py'),'--directory',directory]+(['--crash'] if crash else []),
                    cwd=ROOT,text=True,capture_output=True,timeout=180,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk'),'PYTHONHASHSEED':'0'})
                self.assertEqual(result.returncode,0,result.stderr);fixture=json.loads(result.stdout)
                self.assertEqual(len(fixture['helpers']),15)
                store=w.MemorySink();publish=store.publish
                def lose_ack(events):publish(events);raise OSError('lost acknowledgement')
                with patch.object(store,'publish',side_effect=lose_ack):
                    with self.assertRaises(w.Error):w.sync(fixture['journal'],sink=store)
                count=w.sync(fixture['journal'],sink=store)
                self.assertEqual(w.sync(fixture['journal'],sink=store),count)
                reader=w.Api._from_store(store,entity='e',project='sklearn-replay').run('sklearn-charts')
                actual=reader.history(pandas=False)
                self.assertEqual(actual,fixture['expected']['history'])
                self.assertTrue(all(any(isinstance(v,dict) and v.get('_type') in ['vega','plot'] for v in row.values()) for row in actual))
                final=copy.deepcopy(store.events);store.events=[]
                with self.assertRaises(w.CommError):w.Api._from_store(store,entity='e',project='sklearn-replay').run('sklearn-charts')
                store.events=list(reversed(final))+final
                self.assertEqual(w.Api._from_store(store,entity='e',project='sklearn-replay').run('sklearn-charts').history(pandas=False),actual)
                if os.environ.get('CI') and not crash:
                    output=ROOT/'test-results';output.mkdir(exist_ok=True)
                    (output/'sklearn-training-replay.json').write_text(json.dumps(fixture)+'\n')

    def test_native_import_does_not_log_to_an_already_imported_official_wandb(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        script="""
import importlib,json,sys,tempfile
import wandb as official
sys.path.insert(0,sys.argv[1])
import backprop as w
module=importlib.import_module('backprop.integration.sklearn.plot.classifier')
assert module.wandb is w
assert official is not w and official.run is None
with tempfile.TemporaryDirectory() as directory:
 with w.init(id='native',entity='e',project='native',dir=directory,_sink=w.MemorySink(),_deployment={},settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}) as run:
  module.confusion_matrix([0,1],[0,0]);module.roc([0,1],[[.9,.1],[.2,.8]])
  assert run.summary['confusion_matrix']['_type']=='plot' and run.summary['roc']['_type']=='plot'
assert official.run is None
print(json.dumps({'official_module_untouched':True}))
"""
        result=subprocess.run([sys.executable,'-B','-c',script,str(ROOT/'sdk')],cwd=tempfile.gettempdir(),
            text=True,capture_output=True,timeout=30,env={**os.environ,'PYTHONPATH':str(reference.locate_file(''))})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['official_module_untouched'])
