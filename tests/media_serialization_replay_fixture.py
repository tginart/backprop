"""Generate real media-source relationships for Python/browser replay checks.

Architecture: a committed source -> two shared writers reuse its content ->
delayed and final snapshots. Main caller: test_media_serialization_replay.mjs.
Only temporary local journals and the existing MemorySink are used.
"""
import copy
import json
import os
import tempfile
import backprop as w
from backprop.media_base import Media

with tempfile.TemporaryDirectory() as directory:
    os.environ['BACKPROP_DIR']=directory
    sink=w.MemorySink();api=w.Api._from_store(sink,entity='e',project='media-provenance')
    options=dict(id='shared',entity='e',project='media-provenance',dir=directory,
        _sink=sink,_deployment={},mode='shared')
    settings={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}
    primary=w.Run(**options,settings={**settings,'x_primary':True,'x_label':'primary'})
    secondary=w.Run(**options,settings={**settings,'x_primary':False,'x_label':'secondary'})
    source=w.Artifact('source','dataset');source.add(w.Html('reused source bytes',inject=False),'html')
    primary.log_artifact(source);primary.flush()
    snapshots=[]
    def snapshot():
        expected=[]
        for artifact in api.run('shared').logged_artifacts():
            expected.append({'id':artifact.id,'files':[{'name':entry.path,'sha256':entry.sha256,
                'ref':entry.ref,'birth_artifact_id':entry.birth_artifact_id} for entry in artifact.manifest.entries.values()]})
        snapshots.append({'events':copy.deepcopy(sink.events),'expected':expected})
    snapshot()
    for index,writer in enumerate([primary,secondary]):
        value=api.artifact(source.id).get('html');target=w.Artifact('copy-'+str(index),'dataset')
        Media.to_json(value,target);writer.log_artifact(target)
    secondary.finish();primary.finish();snapshot()
    print(json.dumps({'source_id':source.id,'snapshots':snapshots}))
