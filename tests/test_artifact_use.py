"""Artifact-use argument contracts and acknowledgement-before-input behavior.

Architecture: pinned/native branch matrix -> actual durable upload failures and
retries -> independent artifact/run reads. Main callers: SDK suite and CI.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from backprop.transport import read_journal
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class ArtifactUseContracts(unittest.TestCase):
 def test_pinned_full_signature_and_branch_behavior(self):
  try:reference=importlib.metadata.distribution('wandb')
  except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
  self.assertEqual(reference.version,'0.30.0');records=[]
  for mode in ['reference','native']:
   result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_use_reference_probe.py'),mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=90,
    env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
   self.assertEqual(result.returncode,0,result.stderr);records.append(json.loads(result.stdout))
   if os.environ.get('CI'):
    folder=ROOT/'test-results';folder.mkdir(exist_ok=True);(folder/f'artifact-use-{mode}.json').write_text(result.stdout)
  for label,value in records[0]['offline_config'].items():
   self.assertEqual(value,{'error':'TypeError','message':'Cannot use artifact when in offline mode.'})
   self.assertEqual(records[1]['offline_config'][label],{'accepted':True,'state':'PENDING','identity_preserved':True})
  for label,group in records[0]['imports'].items():
   for key,value in group.items():
    observed=records[1]['imports'][label][key]
    if key=='offline_producer':
     # Native publication preserves the queued producer's best alias before
     # consumer use. The pinned offline producer has not published it remotely.
     self.assertEqual(value['aliases'],['latest'])
     self.assertEqual(observed['aliases'],['best','latest'])
     self.assertEqual({k:v for k,v in value.items() if k!='aliases'},
                      {k:v for k,v in observed.items() if k!='aliases'},(label,key))
    else:self.assertEqual(value,observed,(label,key))

 def test_failed_draft_ack_has_no_successful_input_and_retry_keeps_identity(self):
  with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
   sink=w.MemorySink();run=w.init(id='use',entity='e',project='p',dir=directory,_sink=sink,_deployment={},settings=SETTINGS)
   self.addCleanup(run.finish);artifact=w.Artifact('dataset','data')
   with artifact.new_file('data.txt') as file:file.write('durable')
   publish=sink.publish
   def lost(events):publish(events);raise OSError('ack lost')
   with patch.object(sink,'publish',side_effect=lost):
    with self.assertRaises(w.Error):run.use_artifact(artifact,aliases=['best'])
   before=read_journal(run.sync_dir);self.assertFalse(any(e['type']=='artifact_use' for e in before))
   original=next(e['payload']['artifact_id'] for e in before if e['type']=='artifact')
   self.assertIs(run.use_artifact(artifact,aliases=['best']),artifact)
   self.assertEqual(artifact.state,'COMMITTED');self.assertEqual(artifact.id,original);run.finish()
   api=w.Api._from_store(sink,entity='e',project='p')
   self.assertEqual([a.id for a in api.run('use').used_artifacts()],[original])
   self.assertEqual(api.artifact('dataset:best').get_entry('data.txt')._read_verified_bytes(),b'durable')

 def test_pending_owner_is_preserved_and_aliases_publish_before_input(self):
  with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
   sink=w.MemorySink();owner=w.init(id='owner',entity='e',project='p',dir=directory,_sink=sink,_deployment={},settings=SETTINGS)
   self.addCleanup(owner.finish);artifact=w.Artifact('dataset','data');owner.log_artifact(artifact,aliases=['best'])
   reader=w.init(id='reader',entity='e',project='p',dir=directory,reinit='create_new',_sink=sink,_deployment={},settings=SETTINGS)
   self.addCleanup(reader.finish)
   with patch.object(w,'termwarn') as warn:
    self.assertIs(reader.use_artifact(artifact,type='ignored',aliases=['release'],use_as='deprecated'),artifact)
   self.assertEqual(warn.call_count,2);self.assertEqual(artifact._run_id,'owner')
   reader.finish();owner.finish();api=w.Api._from_store(sink,entity='e',project='p')
   self.assertEqual(api.artifact('dataset:release').id,artifact.id)
   uses=[e['payload'] for e in sink.events if e['type']=='artifact_use'];self.assertEqual(len(uses),1)
   self.assertIsNone(uses[0]['use_as']);self.assertEqual(uses[0]['source_project'],'p')

 def test_queued_alias_lost_ack_replays_before_input_without_changing_owner(self):
  with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
   sink=w.MemorySink();owner=w.init(id='alias-owner',entity='e',project='p',dir=directory,_sink=sink,_deployment={},settings=SETTINGS)
   self.addCleanup(owner.finish);artifact=w.Artifact('dataset','data');owner.log_artifact(artifact)
   reader=w.init(id='alias-reader',entity='e',project='p',dir=directory,reinit='create_new',_sink=sink,_deployment={},settings=SETTINGS)
   self.addCleanup(reader.finish);publish=sink.publish
   def lost(events):
    publish(events)
    if any(e['type']=='artifact_update' for e in events):raise OSError('alias acknowledgement lost')
   with patch.object(sink,'publish',side_effect=lost):
    with self.assertRaises(w.Error):reader.use_artifact(artifact,aliases=['release'])
   self.assertFalse(any(e['type']=='artifact_use' for e in read_journal(reader.sync_dir)))
   pending=read_journal(artifact._edit_writer._journal.directory)
   self.assertEqual(len([e for e in pending if e['type']=='artifact_update']),1)
   self.assertIs(reader.use_artifact(artifact),artifact);reader.finish();owner.finish()
   self.assertEqual(artifact._run_id,'alias-owner')
   api=w.Api._from_store(sink,entity='e',project='p')
   self.assertEqual(api.artifact('dataset:release').id,artifact.id)
   self.assertEqual([a.id for a in api.run('alias-reader').used_artifacts()],[artifact.id])
   updates=[e for e in sink.events if e['type']=='artifact_update']
   self.assertEqual(len(updates),2);self.assertEqual(updates[0],updates[1])
   self.assertEqual(len([e for e in sink.events if e['type']=='artifact_use']),1)
   self.assertIn('release',artifact.aliases)
   artifact.aliases.remove('release');artifact.save()
   with self.assertRaises(w.CommError):api.artifact('dataset:release')
   self.assertEqual(api.artifact('dataset:latest').id,artifact.id)
