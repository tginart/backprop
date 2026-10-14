"""Recover an accepted contribution handle after a real writer process exit.

Architecture: labeled shared writer exits after acceptance -> primary closes ->
new labeled writer recovers the original part -> delayed read remains pending ->
visible completion refreshes the same handle -> input/output/config publication.
Main callers: Python and browser replay tests. The journal bytes remain intact.
"""
import json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
ROOT=Path(__file__).resolve().parents[1]

CHILD=r"""
import json,os,sys
from pathlib import Path
from unittest.mock import patch
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
record=Path(sys.argv[1]);saved=json.loads(record.read_text());mode=sys.argv[2];sink=DiskSink(saved['remote'])
run=w.init(id='shared',entity='e',project='p',group='parts',mode='shared',dir=saved['directory'],
 _sink=sink,_deployment={},settings={**SETTINGS,'x_label':'primary' if mode=='primary' else 'worker','x_primary':mode=='primary'},reinit='create_new')
artifact=w.Artifact('model','model')
with artifact.new_file('primary' if mode=='primary' else 'worker') as out:out.write('primary' if mode=='primary' else 'worker')
if mode=='crash':
 saved['journal']=run.sync_dir;record.write_text(json.dumps(saved));publish=sink.publish
 def crash(events):
  publish(events)
  if any(e['type']=='artifact_part' for e in events):os._exit(73)
 sink.publish=crash;run.upsert_artifact(artifact)
 raise AssertionError('Expected process exit')
elif mode=='primary':
 run.finish_artifact(artifact);artifact.wait();saved['canonical_id']=artifact.id
 record.write_text(json.dumps(saved));run.finish()
else:
 run.upsert_artifact(artifact);read=sink.read_events
 with patch.object(sink,'read_events',side_effect=lambda **scope:[e for e in read(**scope) if e['type']!='artifact']):
  assert artifact.wait() is artifact and artifact.state=='PENDING'
 assert artifact.wait() is artifact and artifact.state=='COMMITTED'
 assert artifact.id==saved['canonical_id']
 assert {name:entry._read_verified_bytes().decode() for name,entry in artifact.manifest.entries.items()}=={'worker':'worker','primary':'primary'}
 run.use_artifact(artifact);run.log_artifact(artifact);run.config['model']=artifact;run.finish()
 saved['recovered_id']=artifact.id;saved['version']=artifact.version;saved['delayed_completion_pending']=True
 record.write_text(json.dumps(saved))
"""


def build():
    """Run genuine process boundaries and return one shared Python/browser view."""
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
        root=Path(directory);record=root/'intent.json';record.write_text(json.dumps({'directory':directory,'remote':directory+'/remote'}))
        def child(mode,expected=0):
            result=subprocess.run([sys.executable,'-B','-c',CHILD,str(record),mode],cwd=directory,
                capture_output=True,text=True,timeout=60,env={**os.environ,'PYTHONPATH':os.pathsep.join([str(ROOT/'sdk'),str(ROOT/'tests')])})
            assert result.returncode==expected,result.stderr
        child('crash',73);saved=json.loads(record.read_text());original=Path(saved['journal'],'events.jsonl').read_bytes()
        child('primary');child('recover');saved=json.loads(record.read_text());sink=DiskSink(saved['remote']);events=sink.read_events()
        assert Path(saved['journal'],'events.jsonl').read_bytes()==original
        assert len({e['payload']['part_id'] for e in events if e['type']=='artifact_part'})==2
        view=w.MemorySink();view.events=list(reversed(events))+events;view.download_file=sink.download_file
        api=w.Api._from_store(view,entity='e',project='p');run=api.run('shared');outputs=list(run.logged_artifacts());inputs=list(run.used_artifacts())
        assert len(outputs)==len(inputs)==1 and outputs[0].id==inputs[0].id==saved['canonical_id']
        assert run.config['model']['artifact_id']==saved['canonical_id']
        return {'events':events,'expected':{'state':run.state,'config':run.config,
            'artifact_id':outputs[0].id,'version':outputs[0].version,'files':sorted(outputs[0].manifest.entries)},
            'hard_exit_recovered':True,'source_journal_unchanged':True,'delayed_completion_pending':saved['delayed_completion_pending']}


if __name__=='__main__':print(json.dumps(build()))
