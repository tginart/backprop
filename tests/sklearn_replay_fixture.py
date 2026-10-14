"""Real sklearn helper outputs for durable recovery and browser comparisons.

Architecture: all 15 helpers and actual estimators -> ordinary run logging ->
unacknowledged journal -> independent SDK read. Main callers: contract tests
and JavaScript replay. --crash exits without finish/flush to test real recovery.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import shutil
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'sdk'))

def fixture(directory,crash=False,identity='sklearn-charts',entity='e',project='sklearn-replay'):
    import numpy as np
    import backprop as w
    from sklearn_reference_probe import cases
    from joblib import parallel_backend
    selection={'plot_classifier':'all_options','plot_clusterer':'kmeans','plot_regressor':'all_options',
        'plot_residuals':'regression','plot_outlier_candidates':'regression','plot_roc':'multiclass','plot_precision_recall':'multiclass'}
    settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}
    with patch.dict(os.environ,{'BACKPROP_DIR':directory}),parallel_backend('threading'):
        run=w.init(id=identity,entity=entity,project=project,dir=directory,mode='offline',_deployment={},settings=settings)
        helpers=[]
        for name,group in cases().items():
            np.random.seed(123)
            label=selection.get(name,'default' if 'default' in group else 'binary' if 'binary' in group else 'options')
            args,kwargs=group[label]();before=run._journal.count
            getattr(w.sklearn,name)(*args,**kwargs)
            after=run._journal.count
            assert after>before,name
            helpers.append(name)
        # Repeat changed controls so replay and browser checks observe both
        # enabled and disabled settings in immutable history snapshots.
        variants=[('plot_classifier','binary'),('plot_clusterer','other'),('plot_roc','options'),
                  ('plot_precision_recall','options'),('plot_silhouette','options'),
                  ('plot_elbow_curve','options'),('plot_feature_importances','all_options')]
        for name,label in variants:
            np.random.seed(123);args,kwargs=cases()[name][label]()
            getattr(w.sklearn,name)(*args,**kwargs)
        if not crash:run.finish()
        # Reset acknowledgements only in a separate reader's copy of events;
        # the journal itself remains untouched for real sync recovery tests.
        copy_directory=Path(directory)/'independent-read'
        shutil.copytree(run._journal.directory,copy_directory,ignore=shutil.ignore_patterns('ack.json'))
        read_store=w.MemorySink();w.sync(copy_directory,sink=read_store)
        records=read_store.events
        reader=w.Api._from_store(read_store,entity=entity,project=project).run(run.id)
        return {'events':records,'helpers':helpers,'journal':str(run._journal.directory),
            'expected':{'state':reader.state,'history':reader.history(pandas=False)}}

def main():
    if not all(importlib.util.find_spec(name) for name in ['sklearn','numpy','pandas','scipy']):
        print(json.dumps({'unavailable':'optional sklearn numerical dependencies are unavailable'}));return
    parser=argparse.ArgumentParser();parser.add_argument('--directory');parser.add_argument('--crash',action='store_true');args=parser.parse_args()
    if args.directory:result=fixture(args.directory,args.crash)
    else:
        with tempfile.TemporaryDirectory() as directory:result=fixture(directory)
    print(json.dumps(result),flush=True)
    if args.crash:os._exit(0)

if __name__=='__main__':main()
