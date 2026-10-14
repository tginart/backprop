"""Keep artifact waiting errors and state gates compatible with pinned W&B.

Architecture: isolated upstream/native observations -> actual accepted journal
with a lost acknowledgment -> zero/negative deadlines -> successful recovery.
Main caller: SDK CI. Existing upload-deadline tests also cover background work.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from backprop.artifact_identity import WaitTimeoutError
from test_artifact_identity import SETTINGS
ROOT=Path(__file__).resolve().parents[1]


class ArtifactWaitContract(unittest.TestCase):
    def test_pinned_errors_arguments_returns_and_aliases(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B unavailable')
        self.assertEqual(reference.version,'0.30.0');observations=[]
        for mode in ['reference','native']:
            process=subprocess.run([sys.executable,'-B',str(ROOT/'tests/artifact_wait_reference_probe.py'),mode],
                cwd=tempfile.gettempdir(),capture_output=True,text=True,timeout=40,
                env={**os.environ,'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(process.returncode,0,process.stderr);observations.append(json.loads(process.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'artifact-wait-{mode}.json').write_text(process.stdout)
        observations[0].pop('source_sha256');self.assertEqual(len(observations[0]),105)
        self.assertEqual(observations[0],observations[1])

    def test_deadline_error_alias_and_committed_timeout_short_circuit(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb.sdk.artifacts.exceptions import WaitTimeoutError as Alias
        self.assertIs(Alias,WaitTimeoutError)
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'BACKPROP_DIR':directory}):
            sink=w.MemorySink()
            with w.init(id='r',entity='e',project='p',mode='online',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
                artifact=run.log_artifact(w.Artifact('model','model'))
                self.assertEqual(artifact.state,'PENDING')
                for timeout in [0,-1]:
                    with self.assertRaisesRegex(Alias,'Artifact upload wait timed out') as caught:artifact.wait(timeout=timeout)
                    self.assertIsInstance(caught.exception,w.Error);self.assertIsInstance(caught.exception,TimeoutError)
                publish=sink.publish
                def lost(events):publish(events);raise OSError('lost acknowledgment')
                with patch.object(sink,'publish',side_effect=lost):
                    with self.assertRaises(w.Error):artifact.wait()
                self.assertEqual(artifact.state,'PENDING')
                self.assertIs(artifact.wait(timeout=5),artifact)
                for timeout in [0,-1,'not a timeout',[]]:self.assertIs(artifact.wait(timeout=timeout),artifact)
                self.assertEqual(len({e['event_id'] for e in sink.events if e['type']=='artifact'}),1)
