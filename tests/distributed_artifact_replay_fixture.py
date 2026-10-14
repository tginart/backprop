"""Recover deferred distributed unions across a real process crash.

Architecture: independent offline worker journals -> first worker sync -> primary
sync crashes after accepting the final -> late worker sync -> primary recovery
reuses frozen union -> next generation consumes the late part. Python and browser
read the same reordered/duplicated envelopes. Main callers: release tests.
"""
import copy,json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]

CHILD=r"""
import json,os,sys
from pathlib import Path
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
record=Path(sys.argv[1]);saved=json.loads(record.read_text());mode=sys.argv[2];sink=DiskSink(saved['remote'])
if mode.startswith('prepare-'):
 label=mode.split('-',1)[1]
 with w.init(id=label,entity='e',project='p',group='workers',mode='offline',dir=saved['directory']+'/'+label,_deployment={},settings=SETTINGS) as run:
  artifact=w.Artifact('model','model')
  with artifact.new_file(label) as out:out.write(label)
  if label=='primary':
   assert run.finish_artifact(artifact,aliases=['primary-alias']) is artifact
   run.config['model']=artifact
  else:assert run.upsert_artifact(artifact,aliases=[label+'-alias']) is artifact
  saved[label]=run.sync_dir
 record.write_text(json.dumps(saved))
elif mode=='crash':
 publish=sink.publish
 def crash(events):
  publish(events)
  if any(e['type']=='artifact' for e in events):os._exit(73)
 sink.publish=crash;w.sync(saved['primary'],sink=sink)
 raise AssertionError('Expected hard exit')
elif mode=='recover':
 w.sync(saved['primary'],sink=sink);w.sync(saved['primary'],sink=sink)
elif mode=='next':
 with w.init(id='next',entity='e',project='p',group='workers',dir=saved['directory']+'/next',_sink=sink,_deployment={},settings=SETTINGS) as run:
  artifact=w.Artifact('model','model')
  with artifact.new_file('primary') as out:out.write('primary')
  assert run.finish_artifact(artifact,aliases=['next-alias']) is artifact
  assert set(artifact.manifest.entries)=={'primary','late'}
  run.config['model']=artifact
else:w.sync(saved[mode],sink=sink)
"""


def build():
    """Execute and compare two durable generations without live process state."""
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
        folder=Path(directory);record=folder/'intent.json';record.write_text(json.dumps({'directory':directory,'remote':directory+'/remote'}))
        def child(mode,code=0):
            process=subprocess.run([sys.executable,'-B','-c',CHILD,str(record),mode],cwd=directory,capture_output=True,text=True,timeout=60,
                env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')})
            assert process.returncode==code,process.stderr
        for label in ['worker','primary','late']:child('prepare-'+label)
        saved=json.loads(record.read_text());original=Path(saved['primary'],'events.jsonl').read_bytes()
        originals={role:Path(saved[role],'events.jsonl').read_bytes() for role in ['worker','primary','late']}
        child('worker');pending=DiskSink(saved['remote']).read_events();child('crash',73)
        sink=DiskSink(saved['remote']);first=next(e for e in sink.read_events() if e['type']=='artifact')
        assert {ref['name'] for ref in first['payload']['files']}=={'worker','primary'}
        child('late');delayed=copy.deepcopy(sink.read_events());child('recover')
        finals=[e for e in sink.read_events() if e['type']=='artifact']
        assert {e['event_id'] for e in finals}=={first['event_id']}
        assert all(e['payload']==first['payload'] for e in finals)
        child('next');assert Path(saved['primary'],'events.jsonl').read_bytes()==original
        events=sink.read_events();view=w.MemorySink();view.events=list(reversed(events))+events;view.download_file=sink.download_file
        api=w.Api._from_store(view,entity='e',project='p');runs={}
        for name,files in [('primary',{'worker','primary'}),('next',{'late','primary'}),('worker',{'worker','primary'}),('late',{'late','primary'})]:
            run=api.run(name);artifact=list(run.logged_artifacts())[0]
            assert set(artifact.manifest.entries)==files
            assert {n:entry._read_verified_bytes().decode() for n,entry in artifact.manifest.entries.items()}=={n:n for n in files}
            if 'model' in run.config:assert run.config['model']['version']==artifact.version
            expected_aliases={'worker-alias','primary-alias'} if name in ['worker','primary'] else {'late-alias','next-alias','latest'}
            assert set(artifact.aliases)==expected_aliases
            for alias in expected_aliases:assert api.artifact('model:'+alias).id==artifact.id
            runs[name]={'state':run.state,'config':run.config,'outputs':[{'artifact_id':artifact.id,'version':artifact.version,'aliases':artifact.aliases,'contribution_ids':next(e['payload']['contribution_ids'] for e in events if e['type']=='artifact' and e['payload']['artifact_id']==artifact.id)}]}
        assert all(Path(saved[role],'events.jsonl').read_bytes()==value for role,value in originals.items())
        return {'events':events,'pending':pending,'delayed':delayed,'runs':runs,'hard_exit_recovered':True,'late_part_retained':True,'unchanged_journal':True,'file_bytes_verified':True}

if __name__=='__main__':print(json.dumps(build()))
