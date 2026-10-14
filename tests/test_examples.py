"""Exercise example entry points and verify durable public-facing outputs.

Architecture: CLI subprocess -> isolated offline journal -> reconstructed events
and model artifact bytes. Optional demos are skipped only if their dependency is
absent. Main caller: unittest discovery. No network or package installation.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'sdk'))
from backprop.transport import read_journal


class ExampleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {'BACKPROP_DIR': directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    def execute(self, script, steps):
        """Launch a documented CLI and return its authored run events.

        Main callers: scenario tests. An isolated process checks real entry-point
        imports and prevents SDK run globals from leaking between demos.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('BACKPROP_', 'WANDB_', 'FOUNDRY_'))}
        env['PYTHONPATH'] = str(ROOT / 'sdk')
        command = [sys.executable, str(ROOT / 'examples' / script), '--steps', str(steps),
                   '--output', directory.name]
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        manifests = list(Path(directory.name).glob('*/result.json'))
        self.assertEqual(len(manifests), 1)
        report = json.loads(manifests[0].read_text())
        self.assertEqual(report['mode'], 'offline')
        events = [read_journal(run['journal']) for run in report['runs']]
        for journal in events:
            self.assertTrue(any(event['type'] == 'run_finish' for event in journal))
        return report, events

    def test_offline_incremental_rows_and_computed_loss(self):
        report, journals = self.execute('offline_sync.py', 5)
        history = [e['payload'] for e in journals[0] if e['type'] == 'history']
        self.assertEqual([h['metrics']['epoch'] for h in history], list(range(5)))
        self.assertLess(history[-1]['metrics']['train/loss'], history[0]['metrics']['train/loss'])
        tables = [h['metrics']['training/checkpoints'] for h in history]
        self.assertEqual([t['row_offset'] for t in tables], list(range(5)))
        self.assertEqual(sum(len(t['data']) for t in tables), 5)

    def test_offline_sync_retry_has_unchanged_cursor_and_no_new_sink_events(self):
        sys.path.insert(0, str(ROOT / 'examples'))
        import offline_sync
        import backprop as wandb
        sink = wandb.MemorySink()
        sync = wandb.sync
        published_counts = []
        def upload(directory):
            count = sync(directory, sink=sink)
            published_counts.append(len(sink.events))
            return count
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(project='offline-test', entity='examples', mode='offline',
                                   execution='offline-fixture', output=Path(directory), steps=4, sync=True)
            with patch.object(wandb, 'sync', upload):
                report = offline_sync.demonstrate(args)
        self.assertGreater(report['acknowledged_events'], 0)
        self.assertEqual(report['acknowledged_events'], report['retry_acknowledged_events'])
        self.assertEqual(published_counts[0], published_counts[1])
        self.assertEqual(len([e for e in sink.events if e['type'] == 'history']), 4)

    @unittest.skipUnless(importlib.util.find_spec('numpy') and importlib.util.find_spec('PIL'), 'requires media extra')
    def test_comparison_produces_four_models_and_rich_outputs(self):
        report, journals = self.execute('seed_demo.py', 6)
        self.assertEqual(len(journals), 4)
        losses = [row['eval_loss'] for row in report['measurements']]
        self.assertEqual(losses, sorted(losses, reverse=True))
        for events in journals:
            history = [e['payload']['metrics'] for e in events if e['type'] == 'history']
            self.assertIn('evaluation/confusion_matrix', history[-1])
            self.assertIn('evaluation/probability_map', history[-1])
            self.assertTrue(any(e['type'] == 'artifact' for e in events))

    @unittest.skipUnless(importlib.util.find_spec('torch') and importlib.util.find_spec('numpy'), 'requires torch extra')
    def test_torch_captures_gradients_and_model_checkpoint(self):
        report, journals = self.execute('torch_training.py', 8)
        metrics = [e['payload']['metrics'] for e in journals[0] if e['type'] == 'history']
        names = {key for row in metrics for key in row}
        self.assertTrue(any(key.startswith('gradients/') for key in names), names)
        self.assertTrue(any(key.startswith('parameters/') for key in names), names)
        self.assertTrue(any(e['type'] == 'artifact' for e in journals[0]))
        checkpoint = Path(report['runs'][0]['journal']).parents[2] / 'checkpoint.pt'
        self.assertTrue(checkpoint.is_file())

    @unittest.skipUnless(importlib.util.find_spec('plotly'), 'requires plots extra')
    def test_plotly_logs_2d_3d_and_source_table(self):
        report, journals = self.execute('plotly_gallery.py', 5)
        metrics = [e['payload']['metrics'] for e in journals[0] if e['type'] == 'history'][-1]
        self.assertEqual(metrics['plots/progress']['_type'], 'plotly')
        self.assertEqual(metrics['plots/parameter_path']['_type'], 'plotly')
        self.assertEqual(len(metrics['tables/trajectory']['data']), 5)

    @unittest.skipUnless(importlib.util.find_spec('numpy') and importlib.util.find_spec('PIL'), 'requires media extra')
    def test_pipeline_downloads_artifacts_and_records_lineage(self):
        sys.path.insert(0, str(ROOT / 'examples'))
        import artifact_pipeline
        import backprop as wandb
        sink = wandb.MemorySink()
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(project='pipeline-test', entity='examples', mode='online',
                                   execution='pipeline-fixture', output=Path(directory), steps=5)
            def start(args, name, **kwargs):
                return wandb.Run(project=args.project, entity=args.entity, name=name,
                                 dir=str(args.output), group=args.execution,
                                 _sink=sink, _deployment={},
                                 settings={'x_disable_stats': True, 'disable_git': True,
                                           'save_code': False, 'console': 'off'}, **kwargs)
            with patch.object(artifact_pipeline, 'start', start):
                report = artifact_pipeline.run_pipeline(args)
            self.assertEqual(len(report['runs']), 3)
            self.assertEqual(json.loads((args.output / 'input-dataset/samples.json').read_text()),
                             json.loads((args.output / 'dataset.json').read_text()))
            self.assertEqual(json.loads((args.output / 'input-model/weights.json').read_text()),
                             json.loads((args.output / 'model.json').read_text()))
            api = wandb.Api._from_store(sink, entity=args.entity, project=args.project, config={})
            model = api.artifact('pipeline-fixture-model:evaluated')
            self.assertEqual(model.metadata['evaluation_accuracy'], report['evaluation']['accuracy'])
            inputs = [event for event in sink.events if event['type'] == 'artifact_use']
            self.assertEqual(len(inputs), 2)
            values = [event['payload']['metrics'] for event in sink.events if event['type'] == 'history']
            self.assertIn('evaluation/predictions', values[-1])
            import inspect_project
            args.group = args.execution
            with patch.object(wandb, 'Api', return_value=api):
                exported = inspect_project.inspect(args)
            self.assertEqual(len(exported), 3)
            self.assertTrue((args.output / 'runs.csv').is_file())
            summary = inspect_project.plain(api.run(report['runs'][2]['id']).summary)
            self.assertIn('eval/accuracy', summary)
            json.dumps(summary)

    def test_all_entry_points_have_help_and_reject_zero_steps(self):
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'sdk')}
        scripts = ['seed_demo.py', 'offline_sync.py', 'resume_training.py', 'artifact_pipeline.py',
                   'shared_training.py', 'torch_training.py', 'plotly_gallery.py', 'inspect_project.py']
        for name in scripts:
            with self.subTest(script=name):
                base = [sys.executable, str(ROOT / 'examples' / name)]
                help_result = subprocess.run([*base, '--help'], env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                if name != 'inspect_project.py':
                    invalid = subprocess.run([*base, '--steps', '0'], env=env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(invalid.returncode, 2)
                    self.assertIn('positive', invalid.stderr)


if __name__ == '__main__':
    unittest.main()
