"""Independent API argument comparisons and real artifact selection.

Architecture: isolated pinned/native boundary matrix -> real stored manifests
and immutable version/alias lookups. Main caller: SDK suite and release CI.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class ArtifactLookupContracts(unittest.TestCase):
 def test_reference_paths_defaults_types_and_errors_at_each_import(self):
  try:reference=importlib.metadata.distribution('wandb')
  except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
  self.assertEqual(reference.version,'0.30.0');records=[]
  for mode in ['reference','native']:
   result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_lookup_reference_probe.py'),mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=45,
    env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
   self.assertEqual(result.returncode,0,result.stderr);records.append(json.loads(result.stdout))
   if os.environ.get('CI'):
    folder=ROOT/'test-results';folder.mkdir(exist_ok=True);(folder/f'artifact-lookup-{mode}.json').write_text(result.stdout)
  for label,group in records[0]['imports'].items():
   for key,value in group.items():self.assertEqual(value,records[1]['imports'][label][key],(label,key))

 def test_real_scope_selection_versions_and_missing_data(self):
  with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'BACKPROP_DIR':temporary}):
   sink=w.MemorySink();artifacts={}
   for entity,project in [('e','p'),('e','other'),('team','other'),('','p')]:
    with w.init(id='owner',entity=entity,project=project,dir=temporary,_sink=sink,_deployment={},settings=SETTINGS) as run:
     artifact=w.Artifact('dataset','data')
     with artifact.new_file('value.txt') as file:file.write(entity+'/'+project)
     run.log_artifact(artifact,aliases=['best',''])
    artifacts[entity,project]=artifact
   api=w.Api._from_store(sink,entity='e',project='p')
   for name,scope in [('dataset:best',('e','p')),('other/dataset:best',('e','other')),
    ('team/other/dataset:best',('team','other')),('/other/dataset:best',('e','other')),
    ('team//dataset:best',('team','p'))]:
    if scope not in artifacts:
     with self.assertRaises(w.CommError):api.artifact(name)
    else:
     artifact=api.artifact(name);self.assertEqual(artifact.id,artifacts[scope].id)
     self.assertEqual(artifact.get_entry('value.txt')._read_verified_bytes().decode(),'/'.join(scope))
   for selector in ['best','',artifacts['e','p'].version]:self.assertEqual(api.artifact('dataset:'+selector).id,artifacts['e','p'].id)
   self.assertEqual(api.artifact(artifacts['e','p'].id).id,artifacts['e','p'].id)
   with self.assertRaisesRegex(w.CommError,"type 'model' specified but this artifact is of type 'data'"):api.artifact('dataset:best',type='model')
   with self.assertRaises(w.CommError):api.artifact('dataset:missing/slash')
   events=sink.events;sink.events=[]
   with self.assertRaises(w.CommError):api.artifact('dataset:best')
   sink.events=list(reversed(events))+events
   self.assertEqual(api.artifact('dataset:best').id,artifacts['e','p'].id)
