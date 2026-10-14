"""Produce real helper config and histogram events for browser/Python parity.

Architecture: public config helper -> run journal -> independent resume -> SDK
readback and browser replay. Main caller: JavaScript contract suite. Snapshots
retain each logged count array independently of later histogram mutation.
"""
import argparse,json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk'))
import backprop as w
from backprop.apis.public.replay import reduce_events

def fixture():
    with tempfile.TemporaryDirectory() as directory:
        sink=w.MemorySink();settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}
        config=w.helper.parse_config(argparse.Namespace(lr=.1,width=16,ignored=True),exclude=['ignored'])
        with w.Run(id='utilities',entity='e',project='replay',dir=directory,_sink=sink,_deployment={},settings=settings,config=config) as run:
            histogram=w.Histogram(np_histogram=([2,3],[0,1,2]));run.log({'weights':histogram})
        with w.Run(id='utilities',entity='e',project='replay',dir=directory,_sink=sink,_deployment={},settings=settings,resume='must') as run:
            histogram.histogram[:]=[4,1];run.log({'weights':histogram})
        reader=w.Api._from_store(sink,entity='e',project='replay').run('utilities')
        return {'events':sink.events,'expected':reduce_events(sink.events),'history':[{k:v for k,v in row.items() if k not in {'_timestamp','_runtime'}} for row in reader.scan_history()]}

if __name__=='__main__':print(json.dumps(fixture()))
