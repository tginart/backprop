"""Race private journals with different visible bases and crash the first writer.

Architecture: two real bases -> two copied offline journals -> independent
processes prepare different candidates -> shared storage chooses one -> a child
exits immediately after that write -> independent recovery -> Python/browser
compare final outputs and config. Main callers: SDK and JavaScript test suites.
Only the Foundry network is replaced by a temporary shared disk store.
"""
import json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


def build():
    with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'BACKPROP_DIR':temporary}):
        root=Path(temporary);remote=root/'remote';sink=DiskSink(remote)
        def run(identity,offline=False):
            return w.init(id=identity,entity='e',project='p',mode='offline' if offline else 'online',dir=temporary,
                _sink=None if offline else sink,_deployment={},settings=SETTINGS,reinit='create_new')
        for identity in ['oldbase','newbase']:
            with run(identity) as base:
                artifact=w.Artifact('model','model')
                with artifact.new_file(identity) as target:target.write(identity)
                base.log_artifact(artifact)
        with run('source',True) as source:
            delta=w.Artifact('model','model',incremental=True)
            with delta.new_file('delta') as target:target.write('delta')
            source.log_artifact(delta)
        copies=[]
        for identity in ['oldview','newview']:
            with run(identity,True) as copy:
                copy.log_artifact(delta);copy.config['model']=delta
            copies.append(copy)
        # The consumer journals must be sufficient after the original disappears.
        shutil.rmtree(source.sync_dir)
        originals=[Path(r.sync_dir,'events.jsonl').read_bytes() for r in copies]
        script="""
import json,os,sys,time
from pathlib import Path
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
sink=DiskSink(sys.argv[2]);identity=sys.argv[3];mode=sys.argv[4]
read=sink.read_events;claim=sink.claim_artifact_resolution
def visible(**filters):
 rows=read(**filters)
 return [e for e in rows if identity!='oldview' or e['run_id']!='newbase']
sink.read_events=visible
if mode=='race':
 def competing(key,candidate):
  folder=Path(sys.argv[2])/'candidates';folder.mkdir(exist_ok=True)
  (folder/(identity+'.json')).write_text(json.dumps(candidate))
  deadline=time.monotonic()+15
  while len(list(folder.glob('*.json')))<2:
   if time.monotonic()>deadline:raise TimeoutError('second candidate did not arrive')
   time.sleep(.01)
  winner=claim(key,candidate)
  if identity=='oldview':os._exit(73)
  return winner
 sink.claim_artifact_resolution=competing
w.sync(sys.argv[1],sink=sink)
"""
        env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')}
        processes=[subprocess.Popen([sys.executable,'-B','-c',script,r.sync_dir,str(remote),r.id,'race'],
            cwd=temporary,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for r in copies]
        for index,process in enumerate(processes):
            stdout,stderr=process.communicate(timeout=40)
            assert process.returncode==(73 if index==0 else 0),stderr
        candidates=[json.loads(path.read_text()) for path in sorted((remote/'candidates').glob('*.json'))]
        assert {tuple(sorted(f['name'] for f in c['payload']['files'])) for c in candidates}=={('delta','oldbase'),('delta','newbase')}
        with run('late') as late:
            artifact=w.Artifact('model','model')
            with artifact.new_file('late') as target:target.write('late')
            late.log_artifact(artifact)
        result=subprocess.run([sys.executable,'-B','-c',script,copies[0].sync_dir,str(remote),copies[0].id,'recover'],
            cwd=temporary,env=env,text=True,capture_output=True,timeout=40)
        assert result.returncode==0,result.stderr
        for run,original in zip(copies,originals):assert Path(run.sync_dir,'events.jsonl').read_bytes()==original
        events=sink.read_events();view=w.MemorySink();view.events=list(reversed(events))+events;view.download_file=sink.download_file
        api=w.Api._from_store(view,entity='e',project='p');artifact=api.artifact(delta._id)
        files={name:entry._read_verified_bytes().decode() for name,entry in artifact.manifest.entries.items()}
        assert set(files) in [{'oldbase','delta'},{'newbase','delta'}]
        for run in copies:
            assert api.run(run.id).config['model']['version']==artifact.version
            assert [a.id for a in api.run(run.id).logged_artifacts()]==[artifact.id]
        return {'events':events,'artifact_id':artifact.id,'version':artifact.version,'files':sorted(files),
            'runs':{r.id:{'config':api.run(r.id).config,'state':api.run(r.id).state} for r in copies},
            'different_candidates':True,'recovered_after_claim_exit':True,'source_removed':True,'unchanged_journals':True}


if __name__=='__main__':print(json.dumps(build()))
