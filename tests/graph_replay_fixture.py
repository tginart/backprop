"""Emit journal snapshots and verified file bytes for shared browser replay.

Architecture: bound graph -> mutation -> restart -> SDK readback -> event and
file fixture. Main callers: JS replay and browser QA. Non-finite graph attributes
stay in the graph file; the event envelope remains ordinary JSON.
"""
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk'))
import backprop as w
from backprop.apis.public.replay import reduce_events

def fixture():
    with tempfile.TemporaryDirectory() as directory:
        sink=w.MemorySink();settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}
        graph=w.Graph();first=graph.add_node(id='a',name='original');first._attributes.update(nan=float('nan'),positive=float('inf'),negative=-float('inf'),text='NaN')
        graph.add_node(id='b',name='output');graph.add_edge(graph['a'],graph['b'])
        with w.Run(id='graph-replay',entity='e',project='graph',dir=directory,_sink=sink,_deployment={},settings=settings) as run:
            graph.bind_to_run(run,'network',0,id_='model');run.log({'graph':graph})
            graph.add_node(id='a',name='replacement');graph.bind_to_run(run,'network',1);run.log({'graph':graph})
        with w.Run(id='graph-replay',entity='e',project='graph',dir=directory,_sink=sink,_deployment={},settings=settings,resume='must') as run:
            graph.add_node(id='c',name='after restart');run.log({'graph':graph})
        api=w.Api._from_store(sink,entity='e',project='graph');rows=list(api.run(run.id).scan_history())
        return {'events':sink.events,'expected':reduce_events(sink.events),
            'history':[{k:v for k,v in row.items() if k not in {'_timestamp','_runtime'}} for row in rows],
            'files':[sink.download_file(row['graph']).decode() for row in rows],
            'node_counts':[len(json.loads(sink.download_file(row['graph']))['nodes']) for row in rows]}

if __name__=='__main__':print(json.dumps(fixture(),allow_nan=False))
