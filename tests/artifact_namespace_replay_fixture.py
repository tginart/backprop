"""Recover an offline cross-project copy after a real consumer process exits.

Architecture: source delta -> destination offline output -> input handoff crash
-> independent transfer replay -> model download -> delayed destination sync.
The source directory is deleted before the handoff. Main callers: Python and
browser tests. Both reducers inspect actual emitted envelopes, including duplicates.
"""
import copy,json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


def build():
    with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'BACKPROP_DIR':temp}):
        folder=Path(temp);sink=DiskSink(folder/'remote')
        with w.init(id='base',entity='e',project='destination',dir=temp,_sink=sink,_deployment={},settings=SETTINGS) as run:
            base=w.Artifact('model','model')
            with base.new_file('base') as out:out.write('base')
            run.log_artifact(base)
        with w.init(id='source',entity='e',project='source',mode='offline',dir=temp,_deployment={},settings=SETTINGS) as source:
            artifact=w.Artifact('model','model',incremental=True)
            with artifact.new_file('weights') as out:out.write('immutable')
            source.log_artifact(artifact,aliases=['original'])
        with w.init(id='copy',entity='e',project='destination',mode='offline',dir=temp,_deployment={},settings=SETTINGS) as copied:
            copied.log_artifact(artifact,aliases=['release']);copied.config['model']=artifact
        original=Path(copied.sync_dir,'events.jsonl').read_bytes();shutil.rmtree(source.sync_dir)
        record=folder/'intent.json';record.write_text(json.dumps({'source':copied.sync_dir,'directory':temp,'remote':str(folder/'remote'),'id':artifact._id}))
        script="""
import json,os,sys
from pathlib import Path
import backprop as w
from backprop.config_artifacts import JournalArtifactStore
from backprop.transport import read_journal
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
path=Path(sys.argv[1]);saved=json.loads(path.read_text());sink=DiskSink(saved['remote'])
if sys.argv[2]=='crash':
 events=read_journal(saved['source']);event=next(e for e in events if e['type']=='artifact')
 artifact=w.Artifact._from_manifest(event['payload'],JournalArtifactStore(saved['source'],events,None),'e','destination','copy');artifact._committed=False
 reader=w.init(id='reader',entity='e',project='reader',dir=saved['directory'],_sink=sink,_deployment={},settings=SETTINGS)
 publish=sink.publish
 def crash(events):
  publish(events)
  if any(e['type']=='artifact' for e in events):
   saved['transfer']=artifact._handoff_journal;path.write_text(json.dumps(saved));os._exit(73)
 sink.publish=crash;reader.use_artifact(artifact)
 raise AssertionError('Expected exit')
else:
 w.sync(saved['transfer'],sink=sink);w.sync(saved['transfer'],sink=sink)
 with w.init(id='reader',entity='e',project='reader',dir=saved['directory'],resume='allow',_sink=sink,_deployment={},settings=SETTINGS) as reader:
  used=reader.use_artifact('e/destination/'+saved['id'])
  assert used.project=='destination' and used._run_id=='copy'
  assert used.get_entry('weights')._read_verified_bytes()==b'immutable'
  from unittest.mock import patch
  with patch('backprop.artifacts.Artifact._default_root',return_value=str(Path(saved['directory'])/'download')):
   result=reader.use_model('e/destination/'+saved['id'])
   assert Path(result,'base').read_bytes()==b'base'
   assert Path(result,'weights').read_bytes()==b'immutable'
"""
        for mode,code in [('crash',73),('recover',0)]:
            child=subprocess.run([sys.executable,'-B','-c',script,str(record),mode],cwd=temp,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')},text=True,capture_output=True,timeout=60)
            assert child.returncode==code,child.stderr
        delayed=copy.deepcopy(sink.read_events())
        w.sync(copied.sync_dir,sink=sink);w.sync(copied.sync_dir,sink=sink)
        assert Path(copied.sync_dir,'events.jsonl').read_bytes()==original
        events=sink.read_events();view=w.MemorySink();view.events=list(reversed(events))+events;view.download_file=sink.download_file
        api=w.Api._from_store(view,entity='e',project='destination');found=api.artifact('model:release')
        assert {name:entry._read_verified_bytes() for name,entry in found.manifest.entries.items()}=={'base':b'base','weights':b'immutable'}
        runs={}
        for project,identity in [('destination','copy'),('reader','reader')]:
            run=api.run(f'e/{project}/{identity}')
            runs[identity]={'project':project,'config':run.config,'state':run.state,'used_artifacts':run._ensure()['used_artifacts'],
                'outputs':[{'artifact_id':a.id,'version':a.version,'aliases':a.aliases,'tags':a.tags} for a in run.logged_artifacts()]}
        assert runs['copy']['config']['model']['source_project']=='destination'
        assert runs['copy']['config']['model']['version']==found.version
        assert found.aliases==['latest','release']
        assert found.logged_by().id=='copy'
        return {'events':events,'delayed_events':delayed,'runs':runs,'artifact_id':found.id,'version':found.version,
            'recovered_after_hard_exit':True,'file_bytes_verified':True,'source_removed':True,'source_journal_unchanged':True}

if __name__=='__main__':print(json.dumps(build()))
