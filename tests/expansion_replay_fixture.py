"""Emit actual SDK events plus expected replay for the JavaScript reducer test.

Architecture: independent writers and public edits -> one durable event fixture;
Node compares its reconstructed values with the public Python view.
"""
import json
import os
import tempfile
import backprop
from backprop.apis.public import Api
from backprop.apis.public.replay import reduce_events
from backprop.transport import MemorySink

with tempfile.TemporaryDirectory() as directory:
    os.environ['BACKPROP_DIR'] = directory
    sink=MemorySink()
    options=dict(project='p',entity='e',id='r',mode='shared',dir=directory,_sink=sink)
    primary=backprop.Run(**options,settings={'x_label':'rank0','x_primary':True,'x_disable_stats':True,'console':'off','silent':True})
    worker=backprop.Run(**options,settings={'x_label':'rank1','x_primary':False,'x_disable_stats':True,'console':'off','silent':True})
    part=backprop.Artifact('shards','dataset',incremental=True)
    with part.new_file('worker') as stream:stream.write('worker')
    worker.upsert_artifact(part,distributed_id='shared-artifact')
    own=backprop.Artifact('shards','dataset',incremental=True)
    with own.new_file('primary') as stream:stream.write('primary')
    final=primary.finish_artifact(own,distributed_id='shared-artifact')
    late=backprop.Artifact('shards','dataset',incremental=True)
    with late.new_file('late') as stream:stream.write('late')
    worker.upsert_artifact(late,distributed_id='shared-artifact')
    final.aliases.append('best');final.tags.append('reviewed');final.save()
    primary.log({'epoch':0,'loss':3}); primary.log({'epoch':1,'loss':2})
    worker.log({'epoch':0,'eval':4}); worker.finish(); primary.flush()
    api=Api._from_store(sink,entity='e',project='p')
    public=api.run('r'); public.config['width']=32; public.name='Edited'; public.summary['review']='ready'; public.update()
    primary.log({'epoch':2,'loss':1}); primary.finish()
    result=reduce_events(sink.events)
    print(json.dumps({'events':sink.events,'expected':{key:result[key] for key in ('config','summary','state','writers')},
        'artifacts':[{'id':a.id,'aliases':a.aliases,'tags':a.tags} for a in api.run('r').logged_artifacts()],
        'pending':[e['payload']['part_id'] for e in sink.events if e['type']=='artifact_part' and e['payload']['manifest']['files'][0]['name']=='late'],
        'history':[{k:v for k,v in row.items() if k not in ('_timestamp','_runtime')} for row in result['history']]}))
