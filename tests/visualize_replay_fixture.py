"""Produce real chart events for browser and SDK replay verification.

Architecture: public Visualize values -> table snapshots/registered definitions
-> shared writers -> independent public reads. Main callers: JavaScript replay,
full browser QA and release evidence. No live credentials or resources are used.
"""
import json
import os
from unittest.mock import patch
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk'))
import backprop as w
from backprop.legacy_charts import VisualizeSpec
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}


def fixture():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
        sink=w.MemorySink();api=w.Api._from_store(sink,entity='e',project='visualize-replay')
        def writer(primary):
            return w.Run(id='charts',name='Legacy chart values',entity='e',project='visualize-replay',tags=['synthetic-demo'],
                mode='shared',dir=directory,_sink=sink,_deployment={},settings={**SETTINGS,'x_primary':primary,'x_label':'rank0' if primary else 'rank1'})
        table=w.Table(columns=['feature_names','importances'],data=[['attention',.7],['projection',.3]],log_mode='MUTABLE')
        chart=w.plot.Visualize(table=table,spec=VisualizeSpec('wandb/feature_importances/v1'))
        with writer(True) as primary:
            primary.log({'importance':chart})
            table.add_data('embedding',.2);primary.log({'importance':chart})
            primary.flush();earlier=list(sink.events)
            with writer(False) as secondary:
                chart.spec=VisualizeSpec('wandb/confusion_matrix/v1')
                chart.table=w.Table(columns=['Predicted','Actual','Count'],data=[['positive','positive',8],['negative','positive',2],['positive','negative',1],['negative','negative',9]])
                secondary.log({'confusion':chart})
            api.create_custom_chart('e','legacy-line','Legacy line','vega2','private',{'data':{'name':'wandb'},'mark':'line',
                'encoding':{'x':{'field':'epoch','type':'quantitative'},'y':{'field':'loss','type':'quantitative'}}})
            chart.spec.name='e/legacy-line';chart.table=w.Table(columns=['epoch','loss'],data=[[0,3],[1,2],[2,1]])
            primary.log({'registered':chart})
        snapshots=[]
        for events in [earlier,sink.events]:
            store=w.MemorySink();store.events=events;store.files=sink.files
            reader=w.Api._from_store(store,entity='e',project='visualize-replay').run('charts')
            snapshots.append({'events':events,'expected':{'state':reader.state,'history':reader.history(pandas=False)}})
        return snapshots


if __name__=='__main__':print(json.dumps(fixture()))
