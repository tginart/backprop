"""Freeze mutable class labels for shared Python/browser reconstruction checks.

Architecture: caller edits retained label lists -> logging takes immutable JSON
snapshots -> both readers replay the existing event schema. Main caller:
test_classes_replay.mjs. Duplicate and reordered delivery must retain each step.
"""
import json
import tempfile
import backprop
from backprop.apis.public.replay import reduce_events

with tempfile.TemporaryDirectory() as directory:
    sink=backprop.MemorySink()
    run=backprop.Run(id='labels',project='classes',entity='e',dir=directory,_sink=sink,_deployment={},
        settings={'console':'off','silent':True,'x_disable_stats':True,'disable_git':True})
    values=[{'id':0,'name':'background'},{'id':1,'name':'foreground'}]
    classes=backprop.Classes(values)
    run.log({'labels':classes})
    values[1]['name']='renamed';run.log({'labels':classes});run.finish()
    values[0]['name']='after-finish'
    state=reduce_events(sink.events)
    print(json.dumps({'events':sink.events,'expected':{key:state[key] for key in ['summary','state']},
        'history':[{k:v for k,v in row.items() if k not in ['_timestamp','_runtime']} for row in state['history']]}))
