"""Verify actual Lightning integration behavior at both declared CI boundaries.

Architecture: independent pinned/native source comparisons -> real Lightning
callbacks and Torch checkpoint files -> Backprop journals -> public readback.
Main callers: optional-training CI and local discovery. Missing frameworks are
explicit skips, never evidence that the corresponding contract passed.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api
from backprop.transport import MemorySink, read_journal

ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = importlib.util.find_spec('lightning') is not None


@unittest.skipUnless(AVAILABLE, 'Optional Lightning Fabric required')
class FabricContracts(unittest.TestCase):
    def setUp(self):
        from backprop.integration.lightning.fabric import WandbLogger
        from lightning.fabric.utilities.rank_zero import rank_zero_only
        self.Logger = WandbLogger
        backprop.teardown()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.addCleanup(backprop.teardown)
        self.directory = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'BACKPROP_DIR': self.temp.name, 'WANDB_MODE': 'online',
            'BACKPROP_MODE': 'online', 'WANDB_PROJECT': 'fabric'}, clear=False).start()
        patch.object(rank_zero_only, 'rank', 0).start()
        self.store = MemorySink()
        self.settings = {'silent': True, 'console': 'off', 'x_disable_stats': True, 'flush_interval': 600}
        self.run = backprop.init(entity='team', project='fabric', dir=self.temp.name,
            _sink=self.store, settings=self.settings)
        self.addCleanup(self.run.finish)
        self.api = Api._from_store(self.store, entity='team', project='fabric')

    def test_full_signatures_branches_and_returns_match_isolated_reference(self):
        outcomes = []
        for kind in ['reference', 'native']:
            process = subprocess.run([sys.executable, '-B', str(ROOT/'tests/fabric_reference_probe.py'), kind],
                env={**os.environ, 'PYTHONPATH': str(ROOT/'sdk')}, capture_output=True, text=True, timeout=60)
            self.assertEqual(process.returncode, 0, process.stderr)
            outcomes.append(json.loads(process.stdout))
            if os.environ.get('CI'):
                output = ROOT/'test-results'; output.mkdir(exist_ok=True)
                (output/f'fabric-{kind}.json').write_text(json.dumps(outcomes[-1], indent=2)+'\n')
        reference, native = outcomes
        old_roots = reference.pop('root_dirs'); new_roots = native.pop('root_dirs')
        self.assertEqual(reference, native)
        self.assertEqual(len(native['routes']), 54)
        self.assertEqual(len(native['artifacts']), 36)
        self.assertEqual(new_roots, [{'value': None}, {'value': None}, {'value': '.'},
                                     {'value': 'runs'}, {'value': '/tmp/experiments'}])
        self.assertTrue(all(row['error']=='AttributeError' for row in old_roots[2:]))

    def test_public_import_aliases_and_properties_do_not_start_a_run(self):
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb.integration.lightning.fabric import WandbLogger as Public
        from wandb.integration.lightning.fabric.logger import WandbLogger as Nested
        self.assertIs(Public, self.Logger); self.assertIs(Nested, self.Logger)
        with patch.object(backprop, 'init', side_effect=AssertionError('property initialized a run')):
            for cls in [Public, Nested, self.Logger]:
                logger = cls(project='project', name='display', version='id', save_dir=Path('root/logs'))
                self.assertEqual(logger.name, 'project'); self.assertEqual(logger.version, 'id')
                self.assertEqual(logger.save_dir, 'root/logs'); self.assertEqual(logger.log_dir, 'root/logs')
                self.assertEqual(logger.root_dir, 'root'); self.assertEqual(logger.group_separator, '-')
                self.assertEqual(logger.LOGGER_JOIN_CHAR, '-')
                self.assertIsNone(logger.finalize('failed')); self.assertIsNone(logger._experiment)
        logger = self.Logger(experiment=self.run, version='ignored')
        self.assertEqual(logger.version, self.run.id)
        self.assertIs(logger.experiment, self.run)

    def test_offline_mode_is_local_and_explicit_mode_wins(self):
        self.run.finish()
        before = os.environ['WANDB_MODE']
        logger = self.Logger(offline=True, save_dir=self.temp.name, settings=self.settings)
        self.assertTrue(logger.experiment.offline)
        self.assertEqual(os.environ['WANDB_MODE'], before)
        logger.experiment.finish()
        explicit = self.Logger(offline=True, mode='disabled', save_dir=self.temp.name, settings=self.settings)
        self.assertTrue(explicit.experiment.disabled); explicit.experiment.finish()
        online = self.Logger(save_dir=self.temp.name, settings=self.settings, _sink=self.store)
        self.assertEqual(online.experiment.mode, 'online'); online.experiment.finish()

    def test_experiment_reuses_existing_run_without_mutating_environment(self):
        logger = self.Logger(offline=True, project='different')
        with self.assertWarns(UserWarning): self.assertIs(logger.experiment, self.run)
        self.assertEqual(os.environ['WANDB_MODE'], 'online')
        self.assertEqual(logger.name, 'different')

    def test_save_commits_pending_values_and_propagates_failures(self):
        logger = self.Logger(experiment=self.run)
        self.run.log({'pending': 4}, commit=False)
        self.assertEqual(self.run.step, 0)
        self.assertIsNone(logger.save()); self.assertEqual(self.run.step, 1)
        self.run.flush()
        rows = list(self.api.run(self.run.id).scan_history())
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['pending'], 4)
        error = RuntimeError('fixture log error')
        with patch.object(self.run, 'log', side_effect=error):
            with self.assertRaises(RuntimeError) as caught: logger.save()
        self.assertIs(caught.exception, error)
        from lightning.fabric.utilities.rank_zero import rank_zero_only
        with patch.object(rank_zero_only, 'rank', 1): self.assertIsNone(logger.save())
        self.assertEqual(self.run.step, 1)

    def callback(self):
        """Build real checkpoint metadata around files produced by torch.save.

        Main callers: checkpoint tests. Trainer internals are unnecessary because
        the adapter consumes the public ModelCheckpoint output fields directly.
        """
        import torch
        from lightning.pytorch.callbacks import ModelCheckpoint
        callback = ModelCheckpoint(dirpath=self.temp.name, monitor='loss', mode='min',
            save_last=True, save_top_k=2, every_n_train_steps=1, save_weights_only=True)
        self.best = self.directory/'best.ckpt'; self.last = self.directory/'last.ckpt'
        torch.save({'weight': torch.tensor([1.0])}, self.best)
        torch.save({'weight': torch.tensor([2.0])}, self.last)
        os.utime(self.best, (100, 100)); os.utime(self.last, (200, 200))
        callback.best_model_path = str(self.best); callback.best_model_score = torch.tensor(.25)
        callback.last_model_path = str(self.last); callback.current_score = torch.tensor(.5)
        callback.best_k_models = {str(self.best): torch.tensor(.25)}
        return callback

    def test_real_checkpoint_aliases_metadata_bytes_and_repeated_files(self):
        import torch
        callback = self.callback()
        logger = self.Logger(experiment=self.run, log_model='all', checkpoint_name='weights')
        self.assertIsNone(logger.after_save_checkpoint(callback)); self.run.flush()
        best = self.api.artifact('weights:best'); latest = self.api.artifact('weights:latest')
        self.assertEqual(best.metadata['score'], .25)
        self.assertEqual(best.metadata['original_filename'], 'best.ckpt')
        self.assertEqual(best.metadata['ModelCheckpoint']['save_top_k'], 2)
        self.assertTrue(best.metadata['ModelCheckpoint']['save_weights_only'])
        self.assertEqual(best.get_entry('model.ckpt')._read_verified_bytes(), self.best.read_bytes())
        self.assertEqual(latest.get_entry('model.ckpt')._read_verified_bytes(), self.last.read_bytes())
        count = len([e for e in read_journal(self.run.sync_dir) if e['type']=='artifact'])
        self.assertEqual(count, 2)
        logger.after_save_checkpoint(callback)
        self.assertEqual(len([e for e in read_journal(self.run.sync_dir) if e['type']=='artifact']), count)
        torch.save({'weight': torch.tensor([3.0])}, self.last); os.utime(self.last, (300, 300))
        logger.after_save_checkpoint(callback); self.run.flush()
        newest = self.api.artifact('weights:latest')
        self.assertNotEqual(newest.id, latest.id)
        self.assertEqual(self.api.artifact('weights:best').id, best.id)
        self.assertEqual(newest.get_entry('model.ckpt')._read_verified_bytes(), self.last.read_bytes())
        self.assertEqual(logger._logged_model_time[str(self.last)], 300)

    def test_checkpoint_failure_retry_and_serialized_progress(self):
        callback = self.callback(); logger = self.Logger(experiment=self.run, log_model='all')
        original = self.run.log_model
        def published_then_failed(*args, **kwargs):
            original(*args, **kwargs); raise OSError('lost acknowledgement')
        with patch.object(self.run, 'log_model', side_effect=published_then_failed):
            with self.assertRaises(OSError): logger.after_save_checkpoint(callback)
        self.assertEqual(logger._logged_model_time, {})
        logger.after_save_checkpoint(callback); self.run.flush()
        self.assertEqual(len(list(self.api.run(self.run.id).logged_artifacts())), 2)
        state = pickle.loads(pickle.dumps(logger.__getstate__()))
        self.assertEqual(state['_logged_model_time'], logger._logged_model_time)
        self.assertIsNone(state['_experiment'])
        self.assertEqual(state['_checkpoint_name'], f'model-{self.run.id}')
        restarted = object.__new__(self.Logger); restarted.__dict__.update(state)
        restarted._experiment = self.run
        with patch.object(self.run, 'log_model', side_effect=AssertionError('unchanged checkpoint retried')):
            restarted.after_save_checkpoint(callback)

    def test_finalize_records_only_requested_status_and_preserves_live_run(self):
        callback = self.callback()
        for status in ['failed', 'interrupted']:
            logger = self.Logger(experiment=self.run, log_model=True)
            logger.after_save_checkpoint(callback); self.assertIsNone(logger.finalize(status))
            self.assertEqual(logger._logged_model_time, {})
        logger = self.Logger(experiment=self.run, log_model=True, log_checkpoint_on='all')
        logger.after_save_checkpoint(callback); self.assertIsNone(logger.finalize('failed'))
        self.assertEqual(len(logger._logged_model_time), 2)
        self.assertFalse(self.run._closed)
        from lightning.fabric.utilities.rank_zero import rank_zero_only
        logger = self.Logger(experiment=self.run, log_model=True)
        logger.after_save_checkpoint(callback)
        with patch.object(rank_zero_only, 'rank', 1): self.assertIsNone(logger.finalize('success'))
        self.assertEqual(logger._logged_model_time, {})
        self.assertIsNone(logger.finalize('success')); self.assertEqual(len(logger._logged_model_time), 2)
        self.assertFalse(self.run._closed)

    def log_artifact(self):
        artifact = backprop.Artifact('weights', 'model')
        with artifact.new_file('weights.txt') as stream: stream.write('checkpoint bytes')
        self.run.log_artifact(artifact, aliases=['best']); self.run.flush()
        return artifact

    def test_artifact_helpers_return_real_files_and_record_usage(self):
        artifact = self.log_artifact(); logger = self.Logger(experiment=self.run)
        used = logger.use_artifact('weights:best', 'model')
        self.assertEqual(used.id, artifact.id)
        destination = self.directory/'download'
        self.assertEqual(self.Logger.download_artifact('weights:best', destination), str(destination))
        self.assertEqual((destination/'weights.txt').read_text(), 'checkpoint bytes')
        self.run.flush()
        self.assertEqual([a.id for a in self.api.run(self.run.id).used_artifacts()], [artifact.id])
        with self.assertRaises(backprop.CommError): logger.use_artifact('weights:best', 'wrong-type')
        before = len([e for e in read_journal(self.run.sync_dir) if e['type']=='artifact_use'])
        with patch.object(backprop, 'Api', return_value=self.api):
            for use in [None, False]:
                target = self.directory/f'without-use-{use}'
                self.assertEqual(self.Logger.download_artifact('weights:best', target, 'model', use), str(target))
            with self.assertRaises(backprop.CommError): self.Logger.download_artifact('weights:best', artifact_type='wrong', use_artifact=False)
            with patch.object(backprop, 'run', None):
                target = self.directory/'no-active'
                self.assertEqual(self.Logger.download_artifact('weights:best', target, 'model'), str(target))
        self.assertEqual(len([e for e in read_journal(self.run.sync_dir) if e['type']=='artifact_use']), before)
        with patch.object(self.run, 'use_artifact', side_effect=OSError('read failure')):
            with self.assertRaises(OSError): logger.use_artifact('weights:best')
            with self.assertRaises(OSError): self.Logger.download_artifact('weights:best')

    @unittest.skipUnless(importlib.util.find_spec('moviepy'), 'Optional video encoder required')
    def test_video_arrays_and_paths_are_encoded_and_read_back(self):
        import numpy as np
        from PIL import Image
        path = self.directory/'input.gif'
        Image.new('RGB', (8, 8), 'red').save(path, save_all=True,
            append_images=[Image.new('RGB', (8, 8), 'blue')], duration=100, loop=0)
        logger = self.Logger(experiment=self.run, prefix='validation')
        self.assertIsNone(logger.log_video('clips', [str(path), np.zeros((2, 3, 8, 8), dtype=np.uint8)],
            step=7, caption=['path', 'array'], format=['gif', 'gif'], fps=[10, 2]))
        self.run.flush()
        values = self.api.run(self.run.id).summary['validation-clips']
        self.assertEqual([value['caption'] for value in values], ['path', 'array'])
        self.assertEqual(self.api.run(self.run.id).summary['trainer/global_step'], 7)
        for value in values:
            reference = value.get('file', value)
            self.assertTrue(self.store.download_file(reference).startswith(b'GIF'))
        self.assertIsNone(logger.log_video('empty', [], step=8))
        self.run.flush(); self.api.flush()
        self.assertEqual(self.api.run(self.run.id).summary['validation-empty'], [])


if __name__ == '__main__': unittest.main()
