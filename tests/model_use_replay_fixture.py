"""Model input/download recovery through the existing event and file stores.

Architecture: child offline producer -> repeated sync -> delayed consumer read
-> download failure -> consumer journal restart -> Python/browser replay.
Main callers: model-use tests and the browser reconstruction test.
"""
import copy,json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'sdk'))
import backprop as w
from test_artifact_identity import SETTINGS


def build():
    """Return events and independently read relationships after real retries.

    Main callers: the Python contract and JavaScript replay suites. Missing
    publication remains an error; neither a missing model nor missing bytes is
    replaced with an empty success result.
    """
    script='''
import json,sys
import backprop as w
from test_artifact_identity import SETTINGS
with w.init(id='producer',entity='e',project='models',mode='offline',dir=sys.argv[1],_deployment={},settings=SETTINGS) as run:
 a=w.Artifact('weights','MODEL')
 with a.new_file('nested/weights.bin',mode='wb') as f:f.write(b'durable model')
 run.log_artifact(a,aliases=['best'])
 print(json.dumps({'journal':run.sync_dir,'id':a._id,'version':a.version}))
'''
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
        result=subprocess.run([sys.executable,'-B','-c',script,directory],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=45,
            env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')})
        if result.returncode:raise AssertionError(result.stderr)
        record=json.loads(result.stdout);sink=w.MemorySink()
        options=dict(id='consumer',entity='e',project='models',dir=directory,_sink=sink,_deployment={},settings=SETTINGS)
        run=w.init(**options)
        try:
            try:run.use_model('weights:best')
            except w.CommError:pass
            else:raise AssertionError('Delayed model incorrectly succeeded')
            if any(e['type']=='artifact_use' for e in sink.events):raise AssertionError('Missing artifact recorded a use')
            w.sync(record['journal'],sink=sink);w.sync(record['journal'],sink=sink)
            attempts=[]
            def failed_read(reference):attempts.append(reference);raise OSError('interrupted model download')
            with patch.object(sink,'download_file',side_effect=failed_read),patch('backprop.artifacts.Artifact._default_root',return_value=str(Path(directory)/'failed')):
                try:run.use_model('weights:best')
                except OSError:pass
                else:raise AssertionError('Missing file bytes were hidden')
            assert attempts
        finally:run.finish()
        restarted=w.init(**options,resume='allow')
        try:
            with patch('backprop.artifacts.Artifact._default_root',return_value=str(Path(directory)/'download')):
                path=w.use_model('weights:'+record['version'])
                assert Path(path).name=='nested'
                assert (Path(path)/'weights.bin').read_bytes()==b'durable model'
        finally:restarted.finish()
        events=copy.deepcopy(sink.events)
        sink.events=list(reversed(events))+events
        api=w.Api._from_store(sink,entity='e',project='models');snapshot=api.run('consumer')
        used=list(snapshot.used_artifacts());assert [a.id for a in used]==[record['id']]
        return {'events':events,'used_artifacts':snapshot._ensure()['used_artifacts'],
            'artifact_id':record['id'],'version':record['version'],'download_verified':True,
            'used_artifact_count':len(used),'download_return':'nested'}


if __name__=='__main__':print(json.dumps(build(),sort_keys=True))
