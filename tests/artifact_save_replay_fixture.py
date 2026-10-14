"""Recover automatic artifact saves across genuine process termination.

Architecture: offline source -> restore its config in a new process -> automatic
save -> exit after backend acceptance before acknowledgment -> remove source ->
independent sync and edits -> Python/browser replay. Only the network boundary
uses DiskSink. Main callers: SDK and browser release tests.
"""
import copy,json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import backprop as w
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]

CHILD = r"""
import json,os,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import backprop as w
from backprop.config_artifacts import hydrate_config
from backprop.transport import read_journal
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
path=Path(sys.argv[1]);saved=json.loads(path.read_text());sink=DiskSink(saved['remote'])
if sys.argv[2]=='crash':
 events=read_journal(saved['source'])
 holder=SimpleNamespace(config=SimpleNamespace(_data={'model':saved['reference']}),entity='e',project='source',_sink=None)
 artifact=hydrate_config(holder,events,saved['source'])['model']
 assert artifact.state=='PENDING'
 original_init=w.init
 def initialize(**kwargs):
  run=original_init(**kwargs,dir=saved['directory'],_sink=sink,_deployment={})
  saved['automatic']=run.sync_dir;path.write_text(json.dumps(saved));return run
 publish=sink.publish
 def crash(events):
  publish(events)
  if any(event['type']=='artifact' for event in events):
   saved['destination_id']=artifact._id;path.write_text(json.dumps(saved));os._exit(73)
 sink.publish=crash
 with patch.object(w,'init',side_effect=initialize):
  artifact.save(project='destination',settings=w.Settings(**SETTINGS,run_id=saved['run']))
 raise AssertionError('Expected hard exit after acceptance')
else:
 w.sync(saved['automatic'],sink=sink);w.sync(saved['automatic'],sink=sink)
 api=w.Api._from_store(sink,entity='e',project='destination')
 artifact=api.artifact(saved['name']+':latest')
 assert artifact.id==saved['destination_id'] and artifact.version==saved['version']
 actual={name:entry._read_verified_bytes().decode() for name,entry in artifact.manifest.entries.items()}
 assert actual==saved['files']
 assert api.run(saved['run']).state=='finished'
 identity=artifact.id;version=artifact.version
 artifact.metadata={'recovered':True};artifact.description='saved after restart'
 artifact.tags=['recovered'];artifact.aliases=['best'];artifact.save(project='ignored',settings=object())
 fresh=api.artifact(saved['name']+':best')
 assert (fresh.id,fresh.version)==(identity,version)
 assert fresh.metadata=={'recovered':True} and fresh.tags==['recovered']
 assert fresh.description=='saved after restart'
"""


def build():
    """Save both file-backed and empty pending artifacts after a hard exit.

    Main callers: Python and browser tests. The source is deleted only after
    the destination durably owns its files; restart must use that destination.
    """
    with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'BACKPROP_DIR':temporary}):
        folder=Path(temporary);sink=DiskSink(folder/'remote');delayed=[]
        for kind in ['file','empty']:
            with w.init(id='source-'+kind,entity='e',project='source',mode='offline',dir=temporary,_deployment={},settings=SETTINGS) as source:
                artifact=w.Artifact('save-'+kind,'model')
                if kind=='file':
                    with artifact.new_file('weights') as out:out.write('immutable')
                source.log_artifact(artifact);source.config['model']=artifact
            source_bytes=Path(source.sync_dir,'events.jsonl').read_bytes()
            intent={'source':source.sync_dir,'reference':artifact._reference_json(),'version':artifact.version,
                    'directory':temporary,'remote':str(folder/'remote'),'run':'auto-'+kind,'name':'save-'+kind,
                    'files':{'weights':'immutable'} if kind=='file' else {}}
            record=folder/(kind+'.json');record.write_text(json.dumps(intent))
            env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')}
            for stage,code in [('crash',73),('recover',0)]:
                child=subprocess.run([sys.executable,'-B','-c',CHILD,str(record),stage],cwd=temporary,env=env,text=True,capture_output=True,timeout=60)
                assert child.returncode==code,child.stderr
                if stage=='crash':
                    assert Path(source.sync_dir,'events.jsonl').read_bytes()==source_bytes
                    delayed.append(copy.deepcopy(sink.read_events()))
                    shutil.rmtree(source.sync_dir)
        events=sink.read_events();view=w.MemorySink();view.events=list(reversed(events))+events;view.download_file=sink.download_file
        api=w.Api._from_store(view,entity='e',project='destination');runs={}
        assert not any(event['project']=='source' for event in events)
        for kind in ['file','empty']:
            run=api.run('auto-'+kind);artifact=api.artifact('save-'+kind+':best')
            assert run.state=='finished' and run.job_type=='auto'
            assert artifact.get_entry('weights')._read_verified_bytes()==b'immutable' if kind=='file' else artifact.file_count==0
            runs[run.id]={'project':run.project,'state':run.state,'config':run.config,
                'outputs':[{'artifact_id':a.id,'version':a.version,'aliases':a.aliases,'tags':a.tags,'metadata':a.metadata,'description':a.description} for a in run.logged_artifacts()]}
        return {'events':events,'delayed':delayed,'runs':runs,'hard_exit_recovery':True,'source_removed':True,
                'source_journals_unchanged':True,'file_bytes_verified':True,'empty_manifest_verified':True}

if __name__=='__main__':print(json.dumps(build()))
