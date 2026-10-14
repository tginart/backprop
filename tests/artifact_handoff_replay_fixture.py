"""Crash recovery and replay using real journals across independent processes.

Architecture: offline producer -> consumer exits after remote acceptance -> new
process syncs the saved transfer -> consumer resume -> delayed producer sync ->
Python/browser replay. Main callers: handoff tests and the JavaScript contract.
Only the Foundry network boundary is replaced by a temporary disk-backed sink.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'sdk'))
import backprop as w
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS


class DiskSink(w.MemorySink):
    """Share a deterministic backend across children without a hosted service."""
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.records=self.directory/'records.jsonl'

    def read_artifact_resolution(self,key):
        path=self.directory/'resolutions'/(key+'.json')
        return json.loads(path.read_text()) if path.exists() else None

    def claim_artifact_resolution(self,key,candidate):
        from backprop.process_lock import exclusive_lock
        from backprop.deferred_branch import _atomic_json
        path=self.directory/'resolutions'/(key+'.json');path.parent.mkdir(exist_ok=True)
        with path.with_suffix('.lock').open('a+b') as lock,exclusive_lock(lock):
            previous=self.read_artifact_resolution(key)
            if previous is not None:return previous
            _atomic_json(path,candidate)
            return json.loads(path.read_text())

    def publish(self,events):
        with self.records.open('a') as target:
            for event in events:target.write(json.dumps(event)+'\n')
            target.flush();os.fsync(target.fileno())

    def read_events(self,**filters):
        if not self.records.exists():return []
        values=[json.loads(line) for line in self.records.read_text().splitlines() if line]
        return [e for e in values if all(value is None or e.get(key)==value for key,value in filters.items())]

    def upload_file(self,path,name):
        # The transfer lock serializes uploads sharing this fixture's journal.
        target=self.directory/'files'/name;target.parent.mkdir(parents=True,exist_ok=True)
        data=Path(path).read_bytes();target.write_bytes(data)
        return {'path':name,'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)}

    def download_file(self,reference):
        data=(self.directory/'files'/reference['path']).read_bytes()
        assert len(data)==reference['size'] and hashlib.sha256(data).hexdigest()==reference['sha256']
        return data

    def control_reader(self,**scope):
        return type('Reader',(),{'poll':lambda self:[],'close':lambda self:None})()


def build():
    """Recover an actually terminated consumer and verify original source intent.

    Main callers: Python and JavaScript suites. Exit 73 occurs after the sink
    accepts the artifact, before its journal can acknowledge the write.
    """
    with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'BACKPROP_DIR':temporary}):
        root=Path(temporary);sink=DiskSink(root/'remote')
        with w.init(id='producer',entity='e',project='source',mode='offline',dir=temporary,_deployment={},settings=SETTINGS) as producer:
            artifact=w.Artifact('weights','MODEL')
            with artifact.new_file('weights.bin',mode='wb') as output:output.write(b'crash-safe model')
            producer.log_artifact(artifact,aliases=['best'])
            producer.log({'private_metric':99})
        source_bytes=Path(producer.sync_dir,'events.jsonl').read_bytes()
        intent={'source':producer.sync_dir,'id':artifact._id,'version':artifact.version,'directory':temporary,'remote':str(root/'remote')}
        record=root/'intent.json';record.write_text(json.dumps(intent))
        script = """
import json,os,sys
from pathlib import Path
import backprop as w
from backprop.config_artifacts import JournalArtifactStore
from backprop.transport import read_journal
from artifact_handoff_replay_fixture import DiskSink
from test_artifact_identity import SETTINGS
saved=json.loads(Path(sys.argv[1]).read_text());sink=DiskSink(saved['remote'])
if sys.argv[2]=='crash':
 events=read_journal(saved['source']);event=next(e for e in events if e['type']=='artifact')
 artifact=w.Artifact._from_manifest(event['payload'],JournalArtifactStore(saved['source'],events,None),'e','source','producer');artifact._committed=False
 reader=w.init(id='consumer',entity='e',project='consumer',dir=saved['directory'],_sink=sink,_deployment={},settings=SETTINGS)
 publish=sink.publish
 def crash(events):
  publish(events)
  if any(e['type']=='artifact' for e in events):
   saved['transfer']=artifact._handoff_journal
   Path(sys.argv[1]).write_text(json.dumps(saved))
   os._exit(73)
 sink.publish=crash
 w.use_artifact(artifact)
 raise AssertionError('Expected hard exit')
else:
 w.sync(saved['transfer'],sink=sink);w.sync(saved['transfer'],sink=sink)
 with w.init(id='consumer',entity='e',project='consumer',dir=saved['directory'],resume='allow',_sink=sink,_deployment={},settings=SETTINGS) as reader:
  from unittest.mock import patch
  with patch('backprop.artifacts.Artifact._default_root',return_value=str(Path(saved['directory'])/'download')):
   path=w.use_model('e/source/weights:'+saved['version'])
   assert Path(path).read_bytes()==b'crash-safe model'
"""
        env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')}
        crashed=subprocess.run([sys.executable,'-B','-c',script,str(record),'crash'],cwd=temporary,env=env,capture_output=True,text=True,timeout=30)
        assert crashed.returncode==73,crashed.stderr
        accepted=sink.read_events()
        assert not any(e['type']=='artifact_use' for e in accepted)
        assert {e['type'] for e in accepted if e['run_id']=='producer'}=={'artifact'}
        restarted=subprocess.run([sys.executable,'-B','-c',script,str(record),'resume'],cwd=temporary,env=env,capture_output=True,text=True,timeout=30)
        assert restarted.returncode==0,restarted.stderr
        delayed=copy.deepcopy(sink.read_events())
        assert Path(producer.sync_dir,'events.jsonl').read_bytes()==source_bytes
        assert producer._journal.acknowledged_count()==0
        w.sync(producer.sync_dir,sink=sink);w.sync(producer.sync_dir,sink=sink)
        events=sink.read_events();view=w.MemorySink();view.events=list(reversed(events))+events
        api=w.Api._from_store(view,entity='e',project='consumer');reader=api.run('consumer')
        used=list(reader.used_artifacts());assert [a.id for a in used]==[artifact._id]
        actual=w.Api._from_store(sink,entity='e',project='source').artifact('weights:best')
        assert actual.get_entry('weights.bin')._read_verified_bytes()==b'crash-safe model'
        return {'events':events,'delayed_events':delayed,'used_artifacts':reader._ensure()['used_artifacts'],
                'artifact_id':artifact._id,'version':artifact.version,'used_artifact_count':len(used),
                'restarted_after_hard_exit':True,'source_was_unsynced_until_explicit_sync':True,'file_bytes_verified':True}


if __name__=='__main__':print(json.dumps(build(),sort_keys=True))
