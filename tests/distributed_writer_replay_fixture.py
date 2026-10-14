"""Verify shared contribution identity after a genuine writer process exit.

Architecture: accepted part and lost acknowledgment -> independent next process
-> inspect both durable cursors -> explicit recovery -> primary close -> duplicate
and reordered Python/browser replay. Labels identify restarted logical writers;
unlabeled processes own distinct journals even when every file byte matches.
Main callers: distributed writer identity tests and browser replay.
"""
import copy,json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w
from backprop.transport import Journal
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]
CHILD=r"""
import json,os,sys
from pathlib import Path
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
root,remote,record,label,action=sys.argv[1:];label=json.loads(label);sink=DiskSink(remote)
run=w.init(id='shared',entity='e',project='p',group='workers',mode='shared',dir=root,_sink=sink,
 _deployment={},settings={**SETTINGS,'x_label':label,'x_primary':False},reinit='create_new')
publish=sink.publish
if action=='crash':
 def lost(events):
  publish(events)
  if any(e['type']=='artifact_part' for e in events):
   Path(record).write_text(json.dumps({'journal':run.sync_dir,'writer':run._writer_id,'ack':run._journal.acknowledged_count()}))
   os._exit(73)
 sink.publish=lost
artifact=w.Artifact('model','model')
with artifact.new_file('same') as out:out.write('same')
run.upsert_artifact(artifact);run.finish()
Path(record).write_text(json.dumps({'journal':run.sync_dir,'writer':run._writer_id,'ack':run._journal.acknowledged_count()}))
"""


def build(label=None):
    """Return replay evidence while asserting journal ownership and exact bytes."""
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
        root=Path(directory);remote=root/'remote';sink=DiskSink(remote)
        env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')}
        def child(name,action):
            record=root/(name+'.json')
            result=subprocess.run([sys.executable,'-B','-c',CHILD,directory,str(remote),str(record),json.dumps(label),action],
                cwd=directory,env=env,capture_output=True,text=True,timeout=30)
            assert result.returncode==(73 if action=='crash' else 0),result.stderr
            return json.loads(record.read_text())
        first=child('first','crash');original=Path(first['journal'],'events.jsonl').read_bytes()
        second=child('second','normal');events=sink.read_events()
        parts={e['payload']['part_id']:e for e in events if e['type']=='artifact_part'}
        count=1 if label else 2
        assert len(parts)==count and first['writer']!=second['writer']
        old_cursor=Journal(first['journal']).acknowledged_count()
        if label:assert old_cursor>first['ack']
        else:
            assert old_cursor==first['ack']
            assert {e['payload']['_writer']['id'] for e in parts.values()}=={first['writer'],second['writer']}
        w.sync(first['journal'],sink=sink)
        assert Path(first['journal'],'events.jsonl').read_bytes()==original
        with w.init(id='shared',entity='e',project='p',group='workers',mode='shared',dir=directory,_sink=sink,
                _deployment={},settings={**SETTINGS,'x_primary':True,'x_label':'primary'},reinit='create_new') as primary:
            artifact=w.Artifact('model','model')
            with artifact.new_file('primary') as out:out.write('primary')
            primary.finish_artifact(artifact);primary.config['model']=artifact
        events=sink.read_events();view=w.MemorySink();view.events=list(reversed(events))+copy.deepcopy(events);view.download_file=sink.download_file
        api=w.Api._from_store(view,entity='e',project='p');run=api.run('shared');artifact=list(run.logged_artifacts())[0]
        assert run.state=='finished' and [a.id for a in run.used_artifacts()]==[artifact.id]
        assert {k:v._read_verified_bytes() for k,v in artifact.manifest.entries.items()}=={'primary':b'primary','same':b'same'}
        manifest=next(e['payload'] for e in events if e['type']=='artifact')
        assert len(set(manifest['contribution_ids']))==count+1
        return {'events':events,'expected':{'state':run.state,'config':run.config,'artifact_id':artifact.id,
            'version':artifact.version,'contribution_ids':manifest['contribution_ids']},
            'unlabeled_journal_isolation':not label,'labeled_restart_recovered':bool(label),
            'hard_exit_recovered':True,'original_journal_unchanged':True,'distinct_contributions':count+1}

if __name__=='__main__':print(json.dumps([build(None),build(''),build('stable-worker')]))
