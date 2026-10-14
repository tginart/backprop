"""Portable helpers tested against the pinned SDK and real persisted logging.

Architecture: isolated reference/native probe -> concrete file/flags/errors ->
normal config/histogram journals -> retries and restart replay. Main callers:
local SDK tests and optional framework CI. No framework is installed here.
"""
import importlib.metadata,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import backprop as w
from backprop.apis.public.replay import reduce_events
from backprop.object_codec import decode_object,encode_object
ROOT=Path(__file__).resolve().parents[1]
SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}

class PortableUtilityContracts(unittest.TestCase):
    def test_full_pinned_matrix_in_independent_processes(self):
        try:reference=importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version,'0.30.0');values=[]
        for mode in ['reference','native']:
            result=subprocess.run([sys.executable,'-B',str(ROOT/'tests/utility_reference_probe.py'),mode],cwd=tempfile.gettempdir(),text=True,capture_output=True,timeout=120,
                env={**os.environ,'WANDB_DATA_DIR':str(Path(tempfile.gettempdir())/'backprop-reference-data'),'PYTHONPATH':str(reference.locate_file('')) if mode=='reference' else str(ROOT/'sdk')})
            self.assertEqual(result.returncode,0,result.stderr);values.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                out=ROOT/'test-results';out.mkdir(exist_ok=True);(out/f'utility-{mode}.json').write_text(result.stdout)
        reference,native=values
        expected_extensions=reference.pop('histogram_extension')
        actual_extensions=native.pop('histogram_extension')
        self.assertEqual(reference,native)
        for module,actual in actual_extensions.items():
            expected=expected_extensions[module]
            self.assertEqual([case['error'] for case in expected['decode']],['TypeError','TypeError','NotImplementedError','NotImplementedError','NotImplementedError','NotImplementedError','TypeError'])
            self.assertEqual(actual['decode'][:2],expected['decode'][:2])
            self.assertEqual(actual['decode'][-1],expected['decode'][-1])
            self.assertEqual(expected['registry_decode'],{'error':'NotImplementedError'})
            self.assertEqual(actual['registry_decode']['value']['decoded_histogram'],{'_type':'histogram','values':[1],'bins':[0,1]})
            for upstream,native_case in zip(expected['roundtrips'],actual['roundtrips']):
                self.assertEqual(upstream['decoded'],{'error':'NotImplementedError'})
                self.assertEqual(native_case['decoded']['value']['decoded_histogram'],upstream['encoded'])
            self.assertEqual([item['error'] for item in actual['invalid']],['TypeError','TypeError','KeyError','KeyError','KeyError','TypeError','ValueError','ValueError'])

    def test_native_helpers_available_without_alias_installation(self):
        result=subprocess.run([sys.executable,'-B','-c',"import backprop; from backprop import helper; assert helper.parse_config({'a':1})=={'a':1}"],cwd=tempfile.gettempdir(),text=True,capture_output=True,env={**os.environ,'PYTHONPATH':str(ROOT/'sdk')})
        self.assertEqual(result.returncode,0,result.stderr)

    def test_deprecation_does_not_initialize_or_contact_authentication(self):
        with patch.object(w,'setup') as setup,patch.object(w,'login') as login,patch.object(w,'termwarn') as warning:
            self.assertIsNone(w.ensure_configured());self.assertIsNone(w.ensure_configured())
            setup.assert_not_called();login.assert_not_called();self.assertEqual(warning.call_count,2)
            self.assertFalse(warning.call_args.kwargs['repeat'])

    def test_anonymous_dump_never_copies_excluded_values(self):
        class Uncopyable:
            def __deepcopy__(self,memo):raise RuntimeError('Must not serialize a deprecated value')
        with patch.object(w,'termwarn'):
            settings=w.Settings(anonymous=Uncopyable())
        self.assertNotIn('anonymous',settings.model_dump());self.assertEqual(settings.model_dump_json(include={'anonymous'}),'{}')
        with self.assertRaises(RuntimeError):settings.model_copy(deep=True)

    def test_helper_config_and_histograms_survive_failed_delivery_and_restart(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as directory:
            sink=w.MemorySink();source=Namespace(lr=.1,width=16)
            config=w.helper.parse_config(source,include=['lr','width'])
            with patch.object(w,'termwarn'):
                settings=w.Settings(**SETTINGS,anonymous='allow')
            run=w.Run(id='utility-contracts',entity='e',project='utilities',dir=directory,_sink=sink,_deployment={},settings=settings,config=config)
            histogram=w.Histogram(np_histogram=([2,3],[0,1,2]));run.log({'weights':histogram})
            # Keep both public and native artifact round trips concrete even
            # though the pinned public decoder itself is abstract.
            artifact=w.Artifact('histogram-values','dataset');payload=encode_object(histogram,artifact)
            restored=decode_object(payload,artifact);self.assertEqual(restored.to_json(),histogram.to_json())
            self.assertEqual(w.Histogram.from_json(payload,artifact).to_json(),histogram.to_json())
            table=w.Table(columns=['histogram'],data=[[histogram]]);artifact.add(table,'measurements');run.log_artifact(artifact)
            publish=sink.publish
            def lost_ack(events):publish(events);raise ConnectionError('lost acknowledgement')
            with patch.object(sink,'publish',side_effect=lost_ack):
                with self.assertRaises(w.Error):run.flush()
            run.flush();run.finish()
            restored_run=w.Run(id=run.id,entity='e',project='utilities',dir=directory,_sink=sink,_deployment={},settings=SETTINGS,resume='must')
            self.addCleanup(restored_run.finish)
            restored_run.log({'weights':w.Histogram(np_histogram=([4,1],[0,1,2]))});restored_run.finish()
            self.assertEqual(reduce_events(sink.events),reduce_events(list(reversed(sink.events))+sink.events))
            api=w.Api._from_store(sink,entity='e',project='utilities');reader=api.run(run.id)
            self.assertEqual(reader.config,{'lr':.1,'width':16})
            self.assertEqual([row['weights']['values'] for row in reader.scan_history()],[[2,3],[4,1]])
            saved=api.artifact('histogram-values:latest').get('measurements')
            self.assertEqual(saved.data[0][0].to_json(),histogram.to_json())
            self.assertNotIn('anonymous',json.dumps(sink.events))

if __name__=='__main__':unittest.main()
