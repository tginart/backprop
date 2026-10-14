"""Verify history wrappers and real event-backed query behavior.

Architecture: isolated pinned comparison -> normal SDK journals -> public reads,
including late visibility, retries, resume, media and independent shared writers.
Main callers: full SDK and optional CI suites. W&B sample selection is server
behavior; native deterministic selection is tested as a documented substitution.
"""
import copy
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {'silent': True, 'console': 'off', 'disable_git': True, 'x_disable_stats': True, 'flush_interval': 600}


class PublicHistoryContracts(unittest.TestCase):
    def test_pinned_argument_result_and_error_matrix_in_separate_processes(self):
        try:
            reference = importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version, '0.30.0')
        results = []
        for mode in ['reference', 'native']:
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/public_history_reference_probe.py'), mode],
                cwd=tempfile.gettempdir(), text=True, capture_output=True, timeout=90,
                env={**os.environ, 'PYTHONPATH': str(reference.locate_file('')) if mode == 'reference' else str(ROOT / 'sdk')})
            self.assertEqual(result.returncode, 0, result.stderr)
            results.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                out = ROOT / 'test-results'; out.mkdir(exist_ok=True)
                (out / f'public-history-{mode}.json').write_text(result.stdout)
        expected, native = results
        self.assertEqual(expected['portable'], native['portable'])
        self.assertEqual(expected['deprecated'], native['deprecated'])
        self.assertEqual(sum(len(item['cases']) for item in native['deprecated'].values()), 14)
        self.assertEqual(sum(len(item['cases']) for item in native['portable'].values()), 78)
        for key, observed in native['native_validation'].items():
            with self.subTest(native_validation=key):
                self.assertEqual(observed['result']['error'], 'CommError')
                self.assertEqual(observed['result']['cause'], 'ValueError')
                self.assertEqual(observed['reads'], 0)
                self.assertEqual(observed['termerror'], [])
        # Actual pinned query helpers serialize the supplied axis, sample
        # count and duplicate requested keys, rather than a hand-written wrapper.
        for module in ['wandb.apis.public', 'wandb.apis.public.runs']:
            requests = expected['reference_requests']
            spec = json.loads(requests[module + '.axis_already_selected'][0]['specs'][0])
            self.assertEqual(spec, {'keys': ['epoch', 'epoch', 'loss', 'loss'], 'samples': 500})
            self.assertEqual(requests[module + '.rows'][0], {'entity': 'team', 'project': 'history', 'name': 'run', 'samples': 500})
            self.assertEqual(requests[module + '.keys_string'], [])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.sink = w.MemorySink()
        self.api = w.Api._from_store(self.sink, entity='e', project='history')

    def writer(self, identity='r', **kwargs):
        run = w.Run(id=identity, entity='e', project='history', dir=self.directory.name,
                    _sink=self.sink, _deployment={}, settings=SETTINGS, **kwargs)
        self.addCleanup(lambda: run.finish() if not run._closed else None)
        return run

    def test_sparse_projection_sampling_and_detached_values(self):
        run = self.writer()
        for step in range(11):
            values = {'epoch': step * 2, 'loss': step, 'weights': w.Histogram(np_histogram=([step, 1], [0, 1, 2]))}
            if step % 2 == 0: values['validation'] = 10 - step
            run.log(values)
        run._emit('system', {'step': 0, 'metrics': {'cpu': 10, 'memory': {'rss': 24}}})
        run.finish(); reader = self.api.run('r')
        self.assertEqual(reader.history(3, ['loss'], 'epoch', False), [{'epoch': 0, 'loss': 0}, {'epoch': 10, 'loss': 5}, {'epoch': 20, 'loss': 10}])
        self.assertEqual(reader.history(1, ['loss'], pandas=False), [{'_step': 10, 'loss': 10}])
        self.assertEqual(reader.history(2, ['validation'], 'epoch', False), [{'epoch': 0, 'validation': 10}, {'epoch': 20, 'validation': 0}])
        self.assertEqual(reader.history(keys=['missing'], pandas=False), [])
        self.assertEqual(len(reader.history(keys=[], x_axis=None, pandas=False)), 11)
        rows = reader.history(samples=2, pandas=False); rows[0]['weights']['values'][0] = -1
        self.assertEqual(reader.history(samples=2, pandas=False)[0]['weights']['values'][0], 0)
        system = reader.history(pandas=False, stream='system')
        self.assertEqual(system[0]['memory.rss'], 24); self.assertEqual(system[0]['cpu'], 10)
        self.assertNotIn('loss', system[0]); self.assertEqual(len(system), 1)
        if importlib.util.find_spec('pandas'):
            frame = reader.history(samples=2, keys=['loss'], x_axis='epoch')
            self.assertEqual(list(frame.columns), ['epoch', 'loss'])
            self.assertEqual(frame.to_dict('records'), [{'epoch': 0, 'loss': 0}, {'epoch': 20, 'loss': 10}])

    def test_refresh_delays_failed_ack_duplicates_and_journal_restart(self):
        run = self.writer(); run.log({'loss': 3}); run.flush()
        visible = copy.deepcopy(self.sink.events)
        reader = self.api.run('r'); self.assertEqual(reader.history(pandas=False)[0]['loss'], 3)
        run.log({'loss': 2})
        publish = self.sink.publish
        def lost_ack(events):
            publish(events); raise ConnectionError('lost acknowledgement')
        with patch.object(self.sink, 'publish', side_effect=lost_ack):
            with self.assertRaises(w.Error): run.flush()
        run.flush(); run.finish()
        with patch.object(self.sink, 'read_events', return_value=visible):
            self.assertEqual([row['loss'] for row in reader.history(pandas=False)], [3])
        self.sink.events = list(reversed(self.sink.events)) + copy.deepcopy(self.sink.events)
        self.assertEqual([row['loss'] for row in reader.history(pandas=False)], [3, 2])
        resumed = self.writer(resume='must'); resumed.log({'loss': 1}); resumed.finish()
        self.assertEqual([row['loss'] for row in reader.history(pandas=False)], [3, 2, 1])
        self.assertEqual([row['_step'] for row in reader.history(pandas=False)], [0, 1, 2])
        before = copy.deepcopy(reader._state)
        with patch.object(self.sink, 'read_events', side_effect=OSError('query failed')):
            with self.assertRaises(w.CommError): reader.history(pandas=False)
        self.assertEqual(reader._state, before)
        self.assertEqual(reader.history(pandas=False), list(reader.scan_history()))
        with patch.object(w, 'termwarn') as warning:
            self.assertEqual(list(reader.beta_scan_history(keys=['loss'], page_size=1, min_step=1, max_step=3, use_cache=False)), [{'loss': 2}, {'loss': 1}])
            self.assertFalse(warning.call_args.kwargs['repeat'])

    def test_only_selected_rich_values_download_and_corruption_is_reported(self):
        run = self.writer()
        run.log({'epoch': 0, 'loss': 2, 'table': w.Table(columns=['text'], data=[['x' * 150_000]])})
        run.log({'epoch': 1, 'loss': 1}); run.finish()
        reader = self.api.run('r')
        with patch.object(self.sink, 'download_file', side_effect=OSError('blob failed')) as download:
            self.assertEqual(reader.history(keys=['loss'], x_axis='epoch', pandas=False), [{'epoch': 0, 'loss': 2}, {'epoch': 1, 'loss': 1}])
            self.assertEqual(reader.history(samples=1, pandas=False)[0]['loss'], 1)
            download.assert_not_called()
            with self.assertRaises(w.CommError): reader.history(pandas=False)
        rows = reader.history(pandas=False)
        self.assertEqual(rows[0]['table']['data'], [['x' * 150_000]])
        rows[0]['table']['data'][0][0] = 'changed'
        self.assertEqual(reader.history(pandas=False)[0]['table']['data'], [['x' * 150_000]])

    def test_independent_processes_keep_writer_steps_and_sparse_training_axes(self):
        processes = []
        for label, primary in [('rank0', 'true'), ('rank1', 'false')]:
            path = str(Path(self.directory.name) / (label + '.json'))
            process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'tests/shared_writer_fixture.py'), self.directory.name, label, primary, path],
                                       cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT / 'sdk')})
            processes.append((process, path))
        for process, path in processes:
            self.assertEqual(process.wait(timeout=60), 0)
            self.sink.events.extend(json.loads(Path(path).read_text()))
        self.sink.events = self.sink.events[::-1] + copy.deepcopy(self.sink.events)
        reader = w.Api._from_store(self.sink, entity='e', project='p').run('shared')
        rows = reader.history(pandas=False)
        self.assertEqual(len(rows), 6); self.assertEqual(len({row['_writer'] for row in rows}), 2)
        for label in ['rank0', 'rank1']:
            selected = [row for row in rows if row['_writer_label'] == label]
            self.assertEqual([row['_step'] for row in selected], [0, 1, 2])
            self.assertEqual(reader.history(keys=[label + '/loss'], x_axis='epoch', pandas=False),
                             [{'epoch': epoch, label + '/loss': 3 - epoch} for epoch in range(3)])
        self.assertEqual(reader.history(keys=['rank0/loss', 'rank1/loss'], x_axis='epoch', pandas=False), [])

    def test_real_missing_import_warns_but_a_broken_dependency_propagates(self):
        from backprop.apis.public.history import dataframe
        rows = [{'_step': 0, 'loss': 1}]
        with patch.dict(sys.modules, {'pandas': None}), patch.object(w, 'termwarn') as warning:
            self.assertIs(dataframe(rows, 'pandas', optional=True), rows)
            warning.assert_called_once_with('Unable to load pandas, call history with pandas=False')
        failure = ModuleNotFoundError('missing internal dependency', name='numpy')
        with patch('backprop.apis.public.history.importlib.import_module', side_effect=failure), patch.object(w, 'termwarn') as warning:
            with self.assertRaises(ModuleNotFoundError) as caught: dataframe(rows, 'pandas', optional=True)
            self.assertIs(caught.exception, failure); warning.assert_not_called()


if __name__ == '__main__': unittest.main()
