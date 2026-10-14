"""Model helper contracts and actual persistent model retrieval.

Architecture: pinned/native method comparison -> event-backed producer/consumer
flows -> disabled and repeated-session dispatch. Main caller: SDK suite and CI.
The isolated reference fixture replaces hosted lookup, never helper behavior.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from test_artifact_identity import SETTINGS

ROOT=Path(__file__).resolve().parents[1]


class ModelUseContracts(unittest.TestCase):
    def test_pinned_helper_arguments_lifecycle_and_download_paths(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel is unavailable')
        self.assertEqual(reference.version,'0.30.0');records=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/model_use_reference_probe.py'),mode],
                cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=90,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);records.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                folder=ROOT/'test-results';folder.mkdir(exist_ok=True)
                (folder/f'model-use-{mode}.json').write_text(result.stdout)
        for label, group in records[0]['imports'].items():
            for key, expected in group.items():
                observed=records[1]['imports'][label][key]
                if label=='wandb.use_model' and key.startswith('finished'):
                    # W&B leaves this module export bound to a finished run.
                    # Native stable dispatch clears the active run instead.
                    self.assertEqual(expected['error'],'UsageError')
                    self.assertEqual(observed,{'error':'Error','message':'You must call wandb.init() before wandb.use_model()','calls':[]})
                else:self.assertEqual(expected,observed,(label,key))
        self.assertEqual(records[0]['preinit'],records[1]['preinit'])
        for key in ['method','missing_argument','unknown_argument']:
            self.assertEqual(records[0]['disabled'][key],records[1]['disabled'][key])
        self.assertEqual(records[0]['disabled']['global']['error'],'Error')
        self.assertEqual(records[1]['disabled']['global'],{'value':None,'type':'NoneType'})
        group=records[1]['imports']['wandb.Run']
        self.assertEqual(group['single_directory']['value'],'<download>/weights')
        self.assertEqual(group['wrong_type']['error'],'AssertionError')
        self.assertEqual(group['wrong_type']['calls'],[{'args':[],'kwargs':{'artifact_or_name':'weights:best'}}])
        self.assertEqual(group['offline']['calls'],[])

    def test_real_lookup_type_error_relationship_and_verified_download(self):
        with tempfile.TemporaryDirectory() as temporary,patch.dict(os.environ,{'BACKPROP_DIR':temporary}):
            sink=w.MemorySink()
            with w.init(id='producer',entity='e',project='models',dir=temporary,_sink=sink,_deployment={},settings=SETTINGS) as run:
                model=w.Artifact('weights','PretrainedMODEL')
                with model.new_file('nested/weights.bin',mode='wb') as file:file.write(b'model bytes')
                run.log_artifact(model,aliases=['best'])
                data=w.Artifact('wrong','dataset');run.log_artifact(data)
            with w.init(id='consumer',entity='e',project='models',dir=temporary,_sink=sink,_deployment={},settings=SETTINGS) as run:
                with patch('backprop.artifacts.Artifact._default_root',return_value=str(Path(temporary)/'download')):
                    path=w.use_model('e/models/weights:best')
                    self.assertEqual(Path(path).name,'nested')
                    self.assertEqual((Path(path)/'weights.bin').read_bytes(),b'model bytes')
                with self.assertRaisesRegex(AssertionError,'must contain the substring'):
                    run.use_model('wrong:latest')
            uses=[e['payload']['artifact_id'] for e in sink.events if e['type']=='artifact_use']
            self.assertEqual(uses,[model.id,data.id])

    def test_stable_global_import_across_disabled_and_later_sessions(self):
        imported=w.use_model
        with tempfile.TemporaryDirectory() as temporary:
            for _ in range(2):
                with w.init(mode='disabled',dir=temporary,_deployment={},settings=SETTINGS) as run:
                    self.assertIsNone(imported())
                    self.assertIsNone(run.use_model(unknown=True))
                with self.assertRaisesRegex(w.Error,'before wandb.use_model'):
                    imported('anything')

    def test_durable_retry_reordered_events_and_consumer_restart(self):
        from model_use_replay_fixture import build
        result=build()
        self.assertTrue(result['download_verified'])
        self.assertEqual(result['used_artifact_count'],1)
        self.assertEqual(len(result['used_artifacts']),2)

    def test_model_helpers_never_execute_pickled_checkpoint_bytes(self):
        import pickle
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'BACKPROP_DIR':temporary}):
            marker = Path(temporary) / 'unexpected-execution'
            class Payload:
                def __reduce__(self):
                    return (eval, ("__import__('pathlib').Path(" + repr(str(marker)) + ").write_text('executed')",))
            payload = pickle.dumps(Payload())
            sink = w.MemorySink()
            with w.init(id='producer', entity='e', project='model-byte-boundary', dir=temporary,
                        _sink=sink, _deployment={}, settings=SETTINGS) as run:
                model = w.Artifact('untrusted-checkpoint', 'model')
                with model.new_file('checkpoint.pkl', mode='wb') as handle:handle.write(payload)
                run.log_artifact(model, aliases=['test'])
            with w.init(id='consumer', entity='e', project='model-byte-boundary', dir=temporary,
                        _sink=sink, _deployment={}, settings=SETTINGS) as run:
                with patch('backprop.artifacts.Artifact._default_root', return_value=str(Path(temporary)/'download')):
                    downloaded = Path(run.use_model('untrusted-checkpoint:test'))
                    if downloaded.is_dir():downloaded = downloaded/'checkpoint.pkl'
                    self.assertEqual(downloaded.read_bytes(), payload)
            self.assertFalse(marker.exists(), 'Model helper executed untrusted checkpoint bytes')
