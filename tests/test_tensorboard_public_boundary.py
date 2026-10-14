"""Verify the inspection API around the separately tested TensorBoard reader.

Architecture: real public Api -> real LoggingRun/journal -> controlled importer
boundary -> public Run snapshot and delayed Foundry refresh. Only event-file
decoding is replaced here; optional TensorBoard fixtures exercise that separately.
Main caller: SDK release suite. No TensorBoard installation or service is needed.
"""
import importlib,os,tempfile,unittest
from pathlib import Path
from runtime_test_case import ContextTestCase
from unittest.mock import patch
import backprop as w
from backprop.compatibility import install_wandb_aliases


class TensorBoardPublicBoundary(ContextTestCase):
    def setUp(self):
        self.directory=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ,{'BACKPROP_DIR':str(self.directory),'WANDB_DIR':str(self.directory),
            'WANDB_RUN_ID':'ambient-run','WANDB_MODE':'offline','WANDB_PROJECT':'ambient-project'}))
        self.enterContext(patch('backprop.settings.in_jupyter',return_value=False))
        self.sink=w.MemorySink()
        install_wandb_aliases()

    def api(self,module='wandb',project='configured'):
        return importlib.import_module(module).Api._from_store(self.sink,entity='owner',project=project)

    @staticmethod
    def import_record(run,root):
        """Stand in for decoded events while preserving actual logging behavior.

        Main caller: sync_tensorboard through the fixture boundary. The real
        journaling layer must finish and expose these measurements to inspection.
        """
        assert run._settings.sync_tensorboard is False
        assert run._tensorboard_historical_import is True
        if root=='invalid':raise w.UnsupportedError('Unsupported TensorBoard plugin: fixture')
        run.log({'global_step':7,'loss':0.25})
        return 1

    def test_returns_public_run_for_each_import_and_ignores_ambient_disabled_transport(self):
        for index,module in enumerate(['wandb','wandb.apis.public','wandb.apis.public.api']):
            api=self.api(module);identifier='import-'+str(index)
            with patch.dict(os.environ,{'WANDB_SYNC_TENSORBOARD':'true'}),patch('backprop.integration.tensorboard.sync',side_effect=self.import_record):
                result=api.sync_tensorboard('events',run_id=identifier,project='requested',entity='chosen')
            self.assertIsInstance(result,w.apis.public.Run)
            self.assertEqual(result.path,['chosen','requested',identifier])
            self.assertEqual(result.state,'finished');self.assertEqual(result.summary['loss'],0.25)
            self.assertIs(api.run('/'.join(result.path)),result)
            self.assertTrue(any(e['type']=='history' and e['run_id']==identifier for e in self.sink.events))

    def test_default_generated_id_and_project_follow_the_selected_public_contract(self):
        for project,expected in [('configured','configured'),('', 'uncategorized')]:
            api=self.api(project=project)
            with patch('backprop.integration.tensorboard.sync',side_effect=self.import_record):
                result=api.sync_tensorboard('events',run_id='',project='',entity='')
            self.assertEqual(result.path[:2],['owner',expected])
            self.assertNotEqual(result.id,'ambient-run');self.assertEqual(len(result.id),8)

    def test_acknowledged_journal_seeds_returned_snapshot_before_query_visibility(self):
        api=self.api();read=self.sink.read_events
        with patch.object(self.sink,'read_events',return_value=[]),patch('backprop.integration.tensorboard.sync',side_effect=self.import_record):
            result=api.sync_tensorboard('events',run_id='delayed')
            self.assertEqual(result.state,'finished');self.assertEqual(result.summary['loss'],0.25)
        self.assertTrue(read(entity='owner',project='configured',run_id='delayed'))
        result.load(force=True);self.assertEqual(result.summary['loss'],0.25)

    def test_importer_errors_propagate_instead_of_returning_a_successful_placeholder(self):
        api=self.api()
        with patch('backprop.integration.tensorboard.sync',side_effect=self.import_record):
            with self.assertRaisesRegex(w.UnsupportedError,'Unsupported TensorBoard plugin'):
                api.sync_tensorboard('invalid',run_id='invalid-source')
        self.assertFalse(any(e['type']=='history' and e['run_id']=='invalid-source' for e in self.sink.events))
        self.assertEqual(api.run('invalid-source').state,'failed')

    def test_finish_releases_hooks_stops_workers_and_propagates_conversion_error(self):
        from test_artifact_identity import SETTINGS
        # The decoder dependency is replaced with the lifecycle boundary in this test.
        with patch('backprop.settings_policy.validate_logging_settings',return_value=[]),patch('backprop.integration.tensorboard.register_run') as register,patch('backprop.integration.tensorboard.release_run') as release:
            run=w.init(id='finish-error',entity='e',project='p',mode='online',dir=str(self.directory),
                settings={**SETTINGS,'sync_tensorboard':True},_sink=self.sink,_deployment={},reinit='create_new')
            self.assertEqual(register.call_count, 2)
            lease = register.call_args_list[0].args[0]
            self.assertIs(register.call_args_list[1].args[0], run)
            release.assert_called_once_with(lease)
            with patch.object(run,'_poll_tensorboard',side_effect=NotImplementedError('Unsupported TensorBoard source')):
                with self.assertRaisesRegex(NotImplementedError,'Unsupported TensorBoard source'):run.finish()
            self.assertEqual(release.call_count, 2)
            release.assert_called_with(run)
            self.assertTrue(run._closed);self.assertTrue(run._stop.is_set())
            self.assertFalse(run._tensorboard_registered)
            self.assertEqual(w.Api._from_store(self.sink,entity='e',project='p').run(run.id).state,'failed')
            run.finish();self.assertEqual(release.call_count, 2)

    def test_later_initialization_error_releases_its_registered_hooks(self):
        from test_artifact_identity import SETTINGS
        with patch('backprop.settings_policy.validate_logging_settings',return_value=[]),patch('backprop.integration.tensorboard.register_run') as register,patch('backprop.integration.tensorboard.release_run') as release,patch('backprop.telemetry.SystemMonitor.start',side_effect=RuntimeError('monitor failure')):
            with self.assertRaisesRegex(RuntimeError,'monitor failure'):
                w.Run(id='init-error',entity='e',project='p',mode='online',dir=str(self.directory),
                    settings={**SETTINGS,'sync_tensorboard':True},_sink=self.sink,_deployment={})
            register.assert_called_once();release.assert_called_once_with(register.call_args.args[0])
