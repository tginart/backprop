"""Verify the complete portable export boundary plus real file lifecycle.

Architecture: independent pinned/native processes -> real journals and Arrow ->
fault injection at file operations -> retries, resumed and shared snapshots.
Main callers: local SDK suite and isolated PyArrow CI environments. Test fixtures
never require W&B credentials or create new Foundry storage resources.
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
from backprop.apis.public import IncompleteRunHistoryError

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {'silent': True, 'console': 'off', 'disable_git': True, 'x_disable_stats': True, 'flush_interval': 600}


@unittest.skipUnless(importlib.util.find_spec('pyarrow'), 'History exports require optional PyArrow')
class PortableHistoryExports(unittest.TestCase):
    def test_full_pinned_matrix_and_three_return_type_imports(self):
        try: reference = importlib.metadata.distribution('wandb')
        except importlib.metadata.PackageNotFoundError: self.skipTest('Pinned W&B wheel unavailable')
        self.assertEqual(reference.version, '0.30.0'); values = []
        for mode in ['reference', 'native']:
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/history_export_reference_probe.py'), mode],
                text=True, capture_output=True, cwd=tempfile.gettempdir(), timeout=120,
                env={**os.environ, 'PYTHONPATH': str(reference.locate_file('')) if mode == 'reference' else str(ROOT / 'sdk')})
            self.assertEqual(result.returncode, 0, result.stderr); values.append(json.loads(result.stdout))
            if os.environ.get('CI'):
                output = ROOT / 'test-results'; output.mkdir(exist_ok=True)
                (output / f'history-export-{mode}.json').write_text(result.stdout)
        self.assertEqual(values[0], values[1])
        self.assertEqual(sum(len(item['cases']) for item in values[0]['exports'].values()), 40)
        self.assertEqual(len(values[0]['returned_objects']), 3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.sink = w.MemorySink()
        self.api = w.Api._from_store(self.sink, entity='e', project='exports')
        self.output = Path(self.temp.name) / 'exports'

    def writer(self, identity='run', *, settings=None, **kwargs):
        run = w.Run(id=identity, entity=kwargs.pop('entity', 'e'), project=kwargs.pop('project', 'exports'),
                    dir=self.temp.name, _sink=self.sink, _deployment={}, settings={**SETTINGS, **(settings or {})}, **kwargs)
        self.addCleanup(lambda: run.finish() if not run._closed else None)
        return run

    def test_atomic_replace_failure_retains_old_file_and_retry_replaces_it(self):
        import pyarrow.parquet as pq
        run = self.writer(); run.log({'loss': 3}); run.finish()
        reader = self.api.run('run'); initial = reader.download_history_exports(self.output)
        destination = initial.paths[0]; before = destination.read_bytes()
        resumed = self.writer(resume='must'); resumed.log({'loss': 2}); resumed.finish()
        for boundary in ['write', 'replace']:
            target = 'pyarrow.parquet.write_table' if boundary == 'write' else 'pathlib.Path.replace'
            with patch(target, side_effect=OSError(boundary + ' failed')):
                failed = reader.download_history_exports(self.output)
            self.assertEqual(failed.paths, []); self.assertEqual(failed.errors, {destination: boundary + ' failed'})
            self.assertEqual(destination.read_bytes(), before)
            self.assertEqual(list(self.output.iterdir()), [destination])
        after = reader.download_history_exports(self.output)
        self.assertEqual(after.paths, [destination]); self.assertEqual(after.errors, {})
        self.assertEqual(pq.read_table(destination).column('loss').to_pylist(), [3, 2])

    def test_missing_rich_file_is_reported_without_publishing_partial_export(self):
        run = self.writer(); run.log({'table': w.Table(columns=['text'], data=[['x' * 150_000]])}); run.finish()
        reader = self.api.run('run')
        with patch.object(self.sink, 'download_file', side_effect=w.Error('missing file')):
            result = reader.download_history_exports(self.output)
        self.assertEqual(result.paths, []); self.assertEqual(list(result.errors.values()), ['missing file'])
        self.assertEqual(list(self.output.iterdir()), [])
        retry = reader.download_history_exports(self.output)
        self.assertEqual(retry.errors, {}); self.assertTrue(retry.paths[0].is_file())

    def test_cleanup_failure_is_reported_with_the_original_file_error(self):
        run = self.writer(); run.log({'loss': 2}); run.finish()
        with patch('pyarrow.parquet.write_table', side_effect=OSError('disk full')):
            with patch('pathlib.Path.unlink', side_effect=OSError('cleanup denied')):
                result = self.api.run('run').download_history_exports(self.output)
        self.assertEqual(result.paths, [])
        self.assertEqual(list(result.errors.values()), ['disk full; Temporary file cleanup failed: cleanup denied'])
        self.assertEqual(len(list(self.output.iterdir())), 1)

    def test_gap_snapshots_keep_export_flag_rows_and_browser_fixture_consistent(self):
        import pyarrow.parquet as pq
        from history_export_replay_fixture import fixture
        for snapshot in fixture():
            with self.subTest(snapshot=snapshot['name']):
                sink = w.MemorySink(); sink.events = snapshot['events']
                reader = w.Api._from_store(sink, entity='e', project='history-export-replay').run('shared-export')
                expected = snapshot['expected']
                if expected['contains_live_data']:
                    with self.assertRaises(IncompleteRunHistoryError): reader.download_history_exports(self.output)
                result = reader.download_history_exports(self.output, not expected['contains_live_data'])
                self.assertEqual(result.contains_live_data, expected['contains_live_data'])
                self.assertEqual(result.errors, {})
                self.assertEqual(pq.read_table(result.paths[0]).to_pylist(), expected['history'])

    def test_setup_and_missing_provider_errors_raise_before_a_file_operation(self):
        run = self.writer(); run.log({'loss': 2}); run.finish(); reader = self.api.run('run')
        with self.assertRaisesRegex(w.CommError, 'directory cannot be empty'): reader.download_history_exports('')
        occupied = Path(self.temp.name) / 'file'; occupied.write_text('keep')
        with self.assertRaises(w.CommError): reader.download_history_exports(occupied)
        self.assertEqual(occupied.read_text(), 'keep')
        with patch.dict(sys.modules, {'pyarrow': None}):
            with self.assertRaisesRegex(ImportError, 'optional pyarrow'): reader.download_history_exports(self.output)
        self.assertFalse(self.output.exists())
        with patch.object(self.sink, 'read_events', side_effect=w.Error('query denied')):
            with self.assertRaisesRegex(w.Error, 'query denied'): reader.download_history_exports(self.output)
        self.assertFalse(self.output.exists())

    def test_two_scopes_with_the_same_id_do_not_overwrite_each_other(self):
        import pyarrow.parquet as pq
        for project, loss in [('exports', 2), ('another', 7)]:
            run = self.writer(project=project); run.log({'loss': loss}); run.finish()
        first = self.api.run('e/exports/run').download_history_exports(self.output)
        second = self.api.run('e/another/run').download_history_exports(self.output)
        self.assertNotEqual(first.paths, second.paths)
        self.assertEqual(pq.read_table(first.paths[0]).column('loss').to_pylist(), [2])
        self.assertEqual(pq.read_table(second.paths[0]).column('loss').to_pylist(), [7])

    def test_empty_run_and_sparse_null_rows_write_real_parquet(self):
        import pyarrow.parquet as pq
        empty = self.writer('empty'); empty.finish()
        result = self.api.run('empty').download_history_exports(self.output)
        self.assertEqual(result.errors, {}); self.assertEqual(pq.read_table(result.paths[0]).num_rows, 0)
        run = self.writer(); run.log({'loss': 2}); run.log({'score': None}); run.finish()
        rows = pq.read_table(self.api.run('run').download_history_exports(self.output).paths[0]).to_pylist()
        self.assertEqual([(row['loss'], row['score']) for row in rows], [(2, None), (None, None)])

    def test_shared_secondary_keeps_history_provisional_after_primary_finishes(self):
        import pyarrow.parquet as pq
        primary = self.writer(mode='shared', settings={'x_primary': True, 'x_label': 'rank0'})
        worker = self.writer(mode='shared', settings={'x_primary': False, 'x_label': 'rank1'})
        primary.log({'epoch': 0, 'loss': 3}); worker.log({'epoch': 0, 'loss': 4}); worker.flush(); primary.finish()
        reader = self.api.run('run'); self.assertEqual(reader.state, 'finished')
        with self.assertRaises(IncompleteRunHistoryError): reader.download_history_exports(self.output)
        allowed = reader.download_history_exports(self.output, require_complete_history=False)
        self.assertTrue(allowed.contains_live_data)
        worker.log({'epoch': 1, 'loss': 2}); worker.finish()
        final = reader.download_history_exports(self.output)
        self.assertFalse(final.contains_live_data)
        rows = pq.read_table(final.paths[0]).to_pylist()
        self.assertEqual(len(rows), 3); self.assertEqual(len({row['_writer'] for row in rows}), 2)
        self.assertEqual([row['_step'] for row in rows if row['_writer_label'] == 'rank1'], [0, 1])

    def test_duplicates_delayed_visibility_and_failed_ack_survive_new_reader(self):
        import pyarrow.parquet as pq
        run = self.writer(); run.log({'loss': 3}); run.flush(); earlier = copy.deepcopy(self.sink.events)
        run.log({'loss': 2})
        publish = self.sink.publish
        def fail_ack(events): publish(events); raise OSError('lost acknowledgement')
        with patch.object(self.sink, 'publish', side_effect=fail_ack):
            with self.assertRaises(w.Error): run.flush()
        run.flush(); run.finish()
        reader = self.api.run('run')
        with patch.object(self.sink, 'read_events', return_value=earlier):
            with self.assertRaises(IncompleteRunHistoryError): reader.download_history_exports(self.output)
            snapshot = reader.download_history_exports(self.output, False)
        self.assertEqual(pq.read_table(snapshot.paths[0]).column('loss').to_pylist(), [3])
        self.sink.events = self.sink.events[::-1] + copy.deepcopy(self.sink.events)
        restarted = w.Api._from_store(self.sink, entity='e', project='exports').run('run')
        result = restarted.download_history_exports(self.output)
        self.assertFalse(result.contains_live_data)
        self.assertEqual(pq.read_table(result.paths[0]).column('loss').to_pylist(), [3, 2])


if __name__ == '__main__': unittest.main()
