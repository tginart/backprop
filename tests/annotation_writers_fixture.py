"""Emit annotation updates from independent shared-run processes.

Architecture: two actual SDK writer processes with separate durable journals ->
combined captured events -> SDK/browser replay. Main callers: annotation writer
tests in Python and JavaScript. No optional array library or network is needed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import backprop
from backprop.media import BoundingBoxes2D
from backprop.apis.public.replay import reduce_events

ROOT=Path(__file__).resolve().parents[1]


def write(label,directory):
    sink=backprop.MemorySink()
    with backprop.Run(id='shared-annotations',entity='e',project='annotations',mode='shared',dir=directory,
            _sink=sink,_deployment={},settings={'silent':True,'console':'off','disable_git':True,
                'x_disable_stats':True,'x_primary':label=='primary','x_label':label}) as run:
        if label=='primary':run.config.update({'_wandb':{'keep':'reserved'}})
        boxes=BoundingBoxes2D({'box_data':[{'position':{'minX':0,'minY':0,'maxX':1,'maxY':1},'class_id':7}],
                              'class_labels':{7:label}},'truth')
        from annotation_replay_fixture import png
        image=backprop.Image(png(),boxes={'truth':boxes},caption=label)
        image.bind_to_run(run,label,0,id_='own')
        image.bind_to_run(run,'comparison',0,id_='common')
    return sink.events


def fixture():
    events=[]
    with tempfile.TemporaryDirectory() as directory:
        for label in ['primary','secondary']:
            result=subprocess.run([sys.executable,'-B',__file__,'--writer',label,directory],capture_output=True,text=True,
                timeout=20,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
            if result.returncode:raise RuntimeError(result.stderr)
            events.extend(json.loads(result.stdout))
    state=reduce_events(events)
    return {'events':events,'config':state['config'],'state':state['state']}


if __name__=='__main__':
    print(json.dumps(write(sys.argv[2],sys.argv[3]) if len(sys.argv)>1 else fixture()))
