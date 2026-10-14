"""Durable alias publication and edits reconstructed by Python and JavaScript.

Architecture: child offline publication -> restart sync -> second writer and
conflicting edits -> delayed/duplicate replay. Main callers: SDK and browser
contract tests. Every event comes from actual run or mutation journals.
"""
import copy,json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'sdk'))
import backprop as w
from test_artifact_identity import SETTINGS


def build():
    """Exercise ownership and recovery without weakening immutable identities.

    Main callers: the alias contract tests. Mutations survive a failed
    acknowledgement by replaying their journal into a fresh sink instance.
    """
    child_script='''
import json,sys
import backprop as w
from test_artifact_identity import SETTINGS
with w.init(id='first',entity='e',project='aliases',dir=sys.argv[1],mode='offline',_deployment={},settings=SETTINGS) as run:
 artifact=w.Artifact('dataset','data')
 with artifact.new_file('value.txt') as file:file.write('first')
 run.log_artifact(artifact,aliases=['','v1','best','duplicate','duplicate'])
 print(json.dumps({'id':artifact._id,'version':artifact.version,'journal':run.sync_dir}),flush=True)
'''
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
        child=subprocess.run([sys.executable,'-B','-c',child_script,directory],cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=45,
            env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')+os.pathsep+str(ROOT/'tests')})
        if child.returncode:raise AssertionError(child.stderr)
        saved=json.loads(child.stdout);sink=w.MemorySink();w.sync(saved['journal'],sink=sink);w.sync(saved['journal'],sink=sink)
        delayed=copy.deepcopy(sink.events)
        api=w.Api._from_store(sink,entity='e',project='aliases')
        stale=api.artifact('dataset:best')
        with w.init(id='second',entity='e',project='aliases',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
            second=w.Artifact('dataset','data')
            with second.new_file('value.txt') as file:file.write('second')
            run.log_artifact(second,aliases=[])
        second=api.artifact('dataset:latest');second.aliases.append('best')
        second.aliases.append(saved['version'])
        second.save()
        stale.aliases.remove('best');stale.aliases.append('recovered')
        publish=sink.publish
        def lost_ack(events):publish(events);raise OSError('acknowledgement lost')
        with patch.object(sink,'publish',side_effect=lost_ack):
            try:stale.save()
            except w.Error:pass
            else:raise AssertionError('Mutation failure was hidden')
        # A fresh store plus the persisted mutation journal replaces the old
        # in-memory edit object, exercising explicit sync/restart recovery.
        recovered=w.MemorySink();recovered.events=copy.deepcopy(sink.events);recovered.files=copy.deepcopy(sink.files)
        journal=stale._edit_writer._journal.directory
        w.sync(journal,sink=recovered);w.sync(journal,sink=recovered)
        events=copy.deepcopy(recovered.events)
        recovered.events=list(reversed(events))+events
        fresh=w.Api._from_store(recovered,entity='e',project='aliases')
        names=['latest','best','','v1',saved['version'],'recovered']
        resolved={name:fresh.artifact('dataset:'+name).id for name in names}
        first=fresh.artifact(saved['id']);second=fresh.artifact(second.id)
        return {'events':events,'delayed_events':delayed,'first_id':first.id,'second_id':second.id,
            'first_version':first.version,'resolved':resolved,
            'python_aliases':{first.id:first.aliases,second.id:second.aliases},
            'mutation_restart_recovered':resolved['recovered']==first.id and resolved['best']==second.id,
            'files':{'first':first.get_entry('value.txt')._read_verified_bytes().decode(),
                     'second':second.get_entry('value.txt')._read_verified_bytes().decode()}}


if __name__=='__main__':print(json.dumps(build(),sort_keys=True))
