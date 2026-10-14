"""Emit native parent/worker records for the browser reconstruction contract.

Architecture: ordinary run promoted at transfer -> independent worker -> parent
restart -> shuffled duplicate event replay. Main caller: test_handoff_replay.mjs.
"""
import json
import tempfile
import backprop
from backprop.apis.public.replay import reduce_events
from backprop.run_handoff import snapshot_run,restore_run
from backprop.transport import MemorySink

with tempfile.TemporaryDirectory() as directory:
    sink=MemorySink()
    options=dict(entity='e',project='p',id='r',dir=directory,_sink=sink,_deployment={},
        settings={'silent':True,'console':'off','x_disable_stats':True,'disable_git':True})
    parent=backprop.Run(**options,config={'lr':.1});parent.log({'epoch':0,'parent':1})
    child=restore_run(snapshot_run(parent));child.log({'epoch':1,'child':2});child.finish()
    assert reduce_events(sink.events)['state']=='running'
    parent.log({'epoch':2,'parent':3});parent.finish()
    resumed=backprop.Run(**options,resume='allow');resumed.log({'epoch':3,'resumed':4});resumed.finish()
    state=reduce_events(sink.events)
    print(json.dumps({'events':sink.events,'expected':{key:state[key] for key in ['summary','config','writers','state']},
        'history':[{k:v for k,v in row.items() if k not in ['_timestamp','_runtime']} for row in state['history']]}))
