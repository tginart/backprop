"""Real process recovery for offline increments and their config references.

Architecture: remote base -> offline delta -> sync process exits after remote
acceptance -> newer version arrives -> new process replays the frozen snapshot
-> Python/browser compare artifacts and config. Main callers: SDK and JS tests.
"""
import json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


def build():
    with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'BACKPROP_DIR':temporary}):
        root=Path(temporary);sink=DiskSink(root/'remote')
        with w.init(id='base',entity='e',project='p',dir=temporary,_sink=sink,_deployment={},settings=SETTINGS) as run:
            base=w.Artifact('model','model')
            with base.new_file('base') as out:out.write('base')
            run.log_artifact(base)
        with w.init(id='offline',entity='e',project='p',mode='offline',dir=temporary,_deployment={},settings=SETTINGS) as offline:
            delta=w.Artifact('model','model',incremental=True)
            with delta.new_file('delta') as out:out.write('delta')
            offline.log_artifact(delta);offline.config['model']=delta
        original=Path(offline.sync_dir,'events.jsonl').read_bytes()
        script="""
import os,sys
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
sink=DiskSink(sys.argv[2]);publish=sink.publish
if sys.argv[3]=='crash':
 def crash(events):
  publish(events)
  if any(e['type']=='artifact' for e in events):os._exit(73)
 sink.publish=crash
w.sync(sys.argv[1],sink=sink);w.sync(sys.argv[1],sink=sink)
"""
        env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')}
        args=[sys.executable,'-B','-c',script,offline.sync_dir,str(root/'remote')]
        result=subprocess.run([*args,'crash'],cwd=temporary,env=env,text=True,capture_output=True,timeout=45)
        assert result.returncode==73,result.stderr
        first=[e for e in sink.read_events() if e['run_id']=='offline']
        assert delta.state=='PENDING'
        with w.init(id='late',entity='e',project='p',dir=temporary,_sink=sink,_deployment={},settings=SETTINGS) as late:
            newer=w.Artifact('model','model')
            with newer.new_file('late') as out:out.write('late')
            late.log_artifact(newer)
        result=subprocess.run([*args,'recover'],cwd=temporary,env=env,text=True,capture_output=True,timeout=45)
        assert result.returncode==0,result.stderr
        assert Path(offline.sync_dir,'events.jsonl').read_bytes()==original
        events=sink.read_events();attempts=[e for e in events if e['run_id']=='offline']
        assert attempts==first+first
        view=w.MemorySink();view.events=list(reversed(events))+events;view.download_file=sink.download_file
        api=w.Api._from_store(view,entity='e',project='p');artifact=api.artifact(delta.id);run=api.run('offline')
        assert set(artifact.manifest.entries)=={'base','delta'}
        assert artifact.get_entry('base')._read_verified_bytes()==b'base'
        assert artifact.get_entry('delta')._read_verified_bytes()==b'delta'
        assert run.config['model']['version']==artifact.version and run.config['model']['digest']==artifact.digest
        return {'events':events,'artifact_id':artifact.id,'version':artifact.version,'config':run.config,
            'state':run.state,'files':sorted(artifact.manifest.entries),'recovered_after_hard_exit':True,
            'original_journal_unchanged':True,'retry_records_identical':True}


if __name__=='__main__':print(json.dumps(build()))
