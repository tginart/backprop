"""Persist actual evaluations for Python/browser agreement on immutable rows.

Architecture: independent shared handles -> durable events and artifacts ->
SDK reconstruction -> browser reconstruction of the same shuffled events/files.
Main caller: test_eval_table_replay.mjs. Files remain in existing MemorySink.
"""
from datetime import date
import json
import base64
import hashlib
import tempfile
import backprop as w
from backprop.apis.public.replay import reduce_events
from backprop.artifact_events import iter_artifact_events
from test_optional_eval_table import SETTINGS


def fixture():
    sink=w.MemorySink();independent_sink=w.MemorySink()
    with tempfile.TemporaryDirectory() as directory:
        for writer in ['secondary','primary']:
            with w.Run(id='evaluation',entity='e',project='eval',mode='shared',dir=directory,_sink=sink,
                       settings={**SETTINGS,'x_primary':writer=='primary','x_label':writer}) as run:
                table=w.EvalTable(columns=['output','score','when','stub'],data=[[writer,1,date(2024,1,1),w.Html('<p>raw</p>')]],score_columns=['score'])
                run.log({writer:table});table.add_data('later',3,date(2024,1,2),w.Html('<p>later</p>'))
                run.log({writer:table})
        disabled=w.Run(id='disabled-evaluation',entity='e',project='eval',mode='disabled',dir=directory,_sink=independent_sink,settings=SETTINGS)
        audio=w.Audio([0.,.25,-.25],8000,caption='Evaluation audio')
        table=w.EvalTable(columns=['answer','audio','score'],data=[['standalone',audio,1.]],score_columns=['score'])
        table.bind_to_run(disabled,'standalone',0);independent_id=table.to_json(disabled)['evaluate_call_id'];disabled.finish()
        independent_artifact=w.Api._from_store(independent_sink,entity='e',project='eval').artifact(independent_id)
        restored=independent_artifact.get('standalone');independent_record=independent_sink.events[0]['payload']
        independent={'events':independent_sink.events,'id':independent_id,'state':reduce_events(independent_sink.events)['state'],
            'files':independent_record['files'],'json':{name:json.loads(entry._read_verified_bytes()) for name,entry in independent_artifact.manifest.entries.items() if name.endswith('.json')},
            'bytes':{name:base64.b64encode(entry._read_verified_bytes()).decode() for name,entry in independent_artifact.manifest.entries.items()},
            'expected':{'answer':restored.data[0][0],'audio_sha256':hashlib.sha256(restored.data[0][1]._bytes()).hexdigest(),'score':restored.data[0][2],'roles':restored.evaluation_roles}}
    api=w.Api._from_store(sink,entity='e',project='eval');public=api.run('evaluation')
    artifacts=[]
    for artifact in public.logged_artifacts():
        record=next(event['payload'] for event in iter_artifact_events(sink.events) if event['payload']['artifact_id']==artifact.id)
        artifacts.append({'files':record['files'],'json':{name:json.loads(entry._read_verified_bytes()) for name,entry in artifact.manifest.entries.items() if name.endswith('.json')}})
    state=reduce_events(sink.events)
    return {'events':sink.events,'summary':state['summary'],'artifacts':artifacts,'state':state['state'],'independent':independent}

if __name__=='__main__':print(json.dumps(fixture()))
