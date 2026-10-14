"""Race two finalizers with different query views and recover a real hard exit.

Architecture: two independent offline journals -> accepted part batches -> child
processes race for a shared closure slot -> one exits after claim -> late part ->
fresh recovery -> next generation. Python/browser consume the same duplicated,
reversed event set. Main callers: distributed closure tests and browser replay.
"""
import copy,json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w
from backprop.transport import Journal,read_journal
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]

CHILD=r"""
import json,os,sys,time
from pathlib import Path
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from backprop.distributed_artifact_closure import KIND
source,remote,name,mode=sys.argv[1:];sink=DiskSink(remote);read=sink.read_events;claim=sink.claim_artifact_resolution
# Every query hides previous completion events. Writer a additionally misses b's
# contribution; each finalizer nevertheless sees its own durable journal prefix.
def visible(**filters):
 return [e for e in read(**filters) if e['type']!='artifact' and (name!='a' or e['run_id']!='b')]
sink.read_events=visible
if mode=='race':
 def compete(key,value):
  if value.get('kind')==KIND:
   folder=Path(remote)/'candidates';folder.mkdir(exist_ok=True)
   marker=folder/(name+'.json')
   if not marker.exists():
    marker.write_text(json.dumps(value));deadline=time.monotonic()+20
    while len(list(folder.glob('*.json')))<2:
     if time.monotonic()>deadline:raise TimeoutError('second finalizer did not arrive')
     time.sleep(.01)
   winner=claim(key,value)
   if name=='a':os._exit(73)
   return winner
  return claim(key,value)
 sink.claim_artifact_resolution=compete
w.sync(source,sink=sink)
"""


def build():
    """Require durable replay and canonical outputs without surviving objects."""
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
        root=Path(directory);remote=root/'remote';sink=DiskSink(remote);runs={}
        def draft(name):
            a=w.Artifact('model','model')
            with a.new_file(name) as out:out.write(name)
            return a
        def run(name,offline=True):
            return w.init(id=name,entity='e',project='p',group='group',mode='offline' if offline else 'online',
                dir=directory,_sink=None if offline else sink,_deployment={},settings=SETTINGS,reinit='create_new')
        for name in ['a','b']:
            with run(name) as writer:
                artifact=draft(name);writer.finish_artifact(artifact);writer.config['model']=artifact
            runs[name]=writer.sync_dir
            journal=Journal(writer.sync_dir,sink);parts=[]
            for e in read_journal(writer.sync_dir,project_artifacts=False):
                if e['type']=='artifact':break
                parts.append(e)
            sink.publish(journal._resolve(parts))
        originals={name:Path(path,'events.jsonl').read_bytes() for name,path in runs.items()}
        env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')}
        def args(name,mode):return [sys.executable,'-B','-c',CHILD,runs[name],str(remote),name,mode]
        processes=[subprocess.Popen(args(name,'race'),env=env,cwd=directory,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for name in ['a','b']]
        for name,process in zip(['a','b'],processes):
            stdout,stderr=process.communicate(timeout=40)
            assert process.returncode==(73 if name=='a' else 0),stderr
        # The late part is not allowed to change a closure already chosen above.
        with run('late',False) as late:late.upsert_artifact(draft('late'))
        for name in ['a','b']:
            process=subprocess.run(args(name,'recover'),env=env,cwd=directory,capture_output=True,text=True,timeout=40)
            assert process.returncode==0,process.stderr
        for name,path in runs.items():assert Path(path,'events.jsonl').read_bytes()==originals[name]
        read=sink.read_events
        with patch.object(sink,'read_events',side_effect=lambda **kw:[e for e in read(**kw) if e['type']!='artifact']):
            with run('next',False) as last:
                final=last.finish_artifact(draft('next'));last.config['model']=final
                assert set(final.manifest.entries)=={'next','late'}
        records=[json.loads(p.read_text()) for p in (remote/'resolutions').glob('*.json')]
        closures=[record for record in records if record.get('kind')=='distributed-artifact-closure']
        parts=[part for record in closures for part in record['resolution']['payload']['contribution_ids']]
        assert len(parts)==len(set(parts))
        events=sink.read_events();view=w.MemorySink();view.events=list(reversed(events))+copy.deepcopy(events);view.download_file=sink.download_file
        api=w.Api._from_store(view,entity='e',project='p');expected={}
        for name in ['a','b','next']:
            public=api.run(name);artifact=list(public.logged_artifacts())[0]
            files={n:entry._read_verified_bytes().decode() for n,entry in artifact.manifest.entries.items()}
            assert name in files and all(k==v for k,v in files.items())
            assert ('late' in files)==(name=='next')
            assert public.config['model']['artifact_id']==artifact.id
            assert [a.id for a in public.used_artifacts()]==[artifact.id]
            inputs=[{key:e['payload'][key] for key in ('artifact_id','name','version','qualified_name')}
                for e in events if e['type']=='artifact_use' and e['run_id']==name]
            assert all(ref==dict(artifact_id=artifact.id,name=artifact.name,version=artifact.version,
                qualified_name=artifact.qualified_name) for ref in inputs)
            expected[name]={'state':public.state,'config':public.config,'inputs':inputs,'outputs':[{'artifact_id':artifact.id,'version':artifact.version,'contribution_ids':next(e['payload']['contribution_ids'] for e in events if e['type']=='artifact' and e['payload']['artifact_id']==artifact.id)}]}
        return {'events':events,'runs':expected,'claim_exit_recovered':True,'disjoint_generations':True,'original_journals_unchanged':True,'exact_file_bytes':True}

if __name__=='__main__':print(json.dumps(build()))
