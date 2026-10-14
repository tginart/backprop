"""Real durable metadata events consumed by Python and the browser model.

Architecture: child publishes an offline journal -> restart sync with a lost
acknowledgement -> ordinary public descriptive edit -> reordered duplicate reads.
Main callers: constructor Python tests and JavaScript reconstruction test.
Why this exists: JSON-like `_type` keys belong to the user and must survive the
same retry/read paths as other metadata without becoming internal media tags.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'sdk'))
import backprop as w
from test_artifact_identity import SETTINGS


def build():
    """Generate actual persisted events and independently verified read results.

    Main callers: SDK and browser replay tests. Temporary directories own both
    publication and mutation journals; no user default directory is modified.
    """
    script = '''
import datetime, enum, json, sys
from pathlib import Path
import backprop as w
from test_artifact_identity import SETTINGS
class Label(enum.Enum):
 FIRST='not-the-enum-name'
with w.init(id='constructor',entity='e',project='constructor',dir=sys.argv[1],mode='offline',_deployment={},settings=SETTINGS) as run:
 artifact=w.Artifact('dataset','data',description='initial',metadata={
  'number':{'_type':'number','value':'Infinity'}, 'timestamp':{'_type':'timestamp','value':12},
  'when':datetime.date(2026,1,2), 'label':Label.FIRST, 'slice':slice(1,7,2),
  'tuple':(1,2), 'bytes':b'hello', 'nan':float('nan'), 7:'numeric-key'})
 path=Path(sys.argv[1])/'data.txt';path.write_bytes(b'unchanged contents')
 artifact.add_file(path);run.log_artifact(artifact)
 print(json.dumps({'journal':run.sync_dir,'id':artifact._id,'version':artifact.version}),flush=True)
'''
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'BACKPROP_DIR':directory}):
        child = subprocess.run([sys.executable,'-B','-c',script,directory],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=45,
            env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')})
        if child.returncode:
            raise AssertionError(child.stderr)
        record = json.loads(child.stdout)
        sink = w.MemorySink()
        publish = sink.publish
        def lost_ack(events):
            publish(events)
            raise OSError('publication acknowledgement was lost')
        with patch.object(sink,'publish',side_effect=lost_ack):
            try:
                w.sync(record['journal'],sink=sink)
            except w.Error:
                pass
            else:
                raise AssertionError('Publication must surface a lost acknowledgement')
        w.sync(record['journal'],sink=sink)
        w.sync(record['journal'],sink=sink)
        api = w.Api._from_store(sink,entity='e',project='constructor')
        artifact = api.artifact(record['id'])
        original = copy.deepcopy(artifact.metadata)
        artifact.metadata['new'] = {'_type':'timestamp','value':'literal metadata'}
        artifact.metadata['today'] = __import__('datetime').date(2026,2,3)
        expected = {**original, 'new':{'_type':'timestamp','value':'literal metadata'}, 'today':'2026-02-03'}
        artifact.description = 'updated'
        with patch.object(sink,'publish',side_effect=lost_ack):
            try:
                artifact.save()
            except w.Error:
                pass
            else:
                raise AssertionError('Descriptive edit must surface a lost acknowledgement')
        artifact.save()
        artifact.metadata['invalid'] = float('inf')
        before = copy.deepcopy(sink.events)
        try:
            artifact.save()
        except ValueError:
            pass
        else:
            raise AssertionError('Infinite metadata must fail before publication')
        unchanged = sink.events == before
        # Re-read through a fresh API after reordering and redelivering events.
        events = copy.deepcopy(sink.events)
        sink.events = list(reversed(events)) + events
        read = w.Api._from_store(sink,entity='e',project='constructor').artifact(record['id'])
        return {'events':events, 'expected':expected, 'python_metadata':read.metadata,
            'artifact_id':read.id, 'version':read.version, 'stable_version':read.version==record['version'],
            'failed_edit_left_snapshot_unchanged':unchanged,
            'immutable_contents_verified':read.get_entry('data.txt')._read_verified_bytes()==b'unchanged contents'}


if __name__ == '__main__':
    print(json.dumps(build(),sort_keys=True))
