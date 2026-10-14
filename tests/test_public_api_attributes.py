"""Public Run return values and refresh semantics from real local SDK journals.

Architecture: logging Run -> MemorySink plus persisted event journal -> public
Run -> unchanged pinned consumer expressions/attribute-loading methods. The
fixtures supply decoded run fields, not a W&B server or guessed opaque schema.
Unknown history-key metadata and native identifiers remain explicit boundaries.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api, RunNotFoundError
from backprop.apis.public.runs import Run as PublicRun, Runs
from backprop.transport import MemorySink
from test_public_api_errors import reference_normalize, supplied_requests

ROOT = Path(__file__).resolve().parents[1] / '.reference/wandb/source'
HASHES = {
    'wandb/apis/public/runs.py': '2d2db69df0175276a3b620094364a848ee2c2857ce123a8656b11e14bddba8da',
    'wandb/apis/attrs.py': 'f1f73ca034d5a153ff46ba09c30295f4143cb3e3689c31e58dfaaae43f41930f',
    'wandb/sdk/verify/verify.py': '31ae200f35b52b92d5b91aff85cacd0286ed55ea6e5f34a03140a1674d184f7a',
    'tools/wandb_export_history.py': 'b5b5648b0ba49dedcbbe7470b7e0400ba4b6b2f2fd56729c5dc0fb3f20165c52',
}


def pinned_tree(name):
    path = ROOT / name
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != HASHES[name]:
        raise AssertionError(f'Unexpected W&B0.30.0 source: {path}')
    return ast.parse(data), str(path)


def pinned_methods(class_name, names):
    """Compile exact selected methods with their actual decorator preserved."""
    tree, path = pinned_tree('wandb/apis/public/runs.py')
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    nodes = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    nodes += [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_convert_to_dict']
    namespace = {'json': json, 'normalize_exceptions': reference_normalize(),
                 'WANDB_INTERNAL_KEYS': {'_wandb', 'wandb_version'},
                 'RUN_FRAGMENT': 'full', 'RUN_FRAGMENT_NAME': 'full',
                 'LIGHTWEIGHT_RUN_FRAGMENT': 'light', 'LIGHTWEIGHT_RUN_FRAGMENT_NAME': 'light',
                 '_create_runs_query': lambda lazy: ('query', lazy)}
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[]))
    exec(compile(module, path, 'exec'), namespace)
    return namespace


class JournalFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sink = MemorySink()
        self.writers = []
        self.api = Api._from_store(self.sink, entity='team', project='attributes')

    def writer(self, id='run', **kwargs):
        run = backprop.Run(project='attributes', entity='team', id=id, dir=self.temp.name,
                           _sink=self.sink, settings={'silent': True, 'console': 'off',
                                                       'disable_git': True, 'disable_code': True}, **kwargs)
        self.writers.append(run)
        self.addCleanup(lambda: run.finish() if not run._closed else None)
        return run

class PublicRunAttributeContracts(JournalFixture, unittest.TestCase):
    def test_history_metadata_is_mapping_with_sparse_previous_values_and_detached_data(self):
        writer = self.writer()
        writer.log({'loss': 3, 'nested': {'score': 1}, 'name': 'ready', 'nullable': 4})
        writer.log({'loss': 2, 'nullable': None})
        writer.finish()
        reader = self.api.run('run')
        with patch.object(self.sink, 'download_file', side_effect=AssertionError('metadata must not download media')):
            keys = reader.history_keys
            self.assertIsInstance(keys, dict)
            self.assertEqual(keys['lastStep'], 1)
            self.assertEqual(keys['keys']['loss']['previousValue'], 2)
            self.assertEqual(keys['keys']['nested.score']['previousValue'], 1)
            self.assertEqual(keys['keys']['name']['previousValue'], 'ready')
            self.assertIsNone(keys['keys']['nullable']['previousValue'])
            keys['keys']['loss']['previousValue'] = 1000
            self.assertEqual(reader.history_keys['keys']['loss']['previousValue'], 2)
            self.assertEqual(reader.load()['historyKeys'], reader.history_keys)

    def test_full_attributes_contain_documented_config_summary_and_system_fields(self):
        writer = self.writer(config={'lr': .01}, name='named', tags=['tag'], notes='note', group='group', job_type='train')
        writer.log({'loss': 3})
        writer.summary['loss'] = 7
        writer._emit('system', {'step': 0, 'metrics': {'cpu': 12, 'nested': {'memory': 8}}})
        writer.finish()
        reader = self.api.run('run')
        with patch.object(self.sink, 'download_file', side_effect=AssertionError('loaded attrs must not download media')):
            attrs = reader.load_full_data()
        self.assertEqual(attrs['config'], {'lr': .01})
        self.assertEqual(attrs['rawconfig']['lr'], .01)
        self.assertEqual(attrs['summaryMetrics']['loss'], 7)
        self.assertEqual(attrs['systemMetrics']['cpu'], 12)
        self.assertEqual(attrs['systemMetrics']['nested.memory'], 8)
        self.assertEqual(attrs['historyKeys']['keys']['loss']['previousValue'], 3)
        self.assertEqual(attrs['historyLineCount'], 1)
        self.assertEqual((attrs['name'], attrs['displayName'], attrs['group'], attrs['jobType'], attrs['notes']),
                         ('run', 'named', 'group', 'train', 'note'))
        self.assertFalse(attrs['readOnly'])
        self.assertEqual(attrs['id'], reader.storage_id)
        self.assertNotIn('summary_metrics', attrs)
        self.assertNotIn('user', attrs)  # No invented account/server records.
        attrs['config']['lr'] = 10
        attrs['summaryMetrics']['loss'] = 100
        self.assertEqual(reader.config['lr'], .01)
        self.assertEqual(reader.summary['loss'], 7)
        # The data came through actual journal publication, not fabricated
        # public read-model fields. System records share the same envelope.
        self.assertTrue(any(event['type'] == 'system' for event in self.sink.events))
        self.assertTrue(list(Path(self.temp.name).rglob('events.jsonl')))

    def test_empty_and_rich_metadata_remain_readable_without_blob_downloads(self):
        empty = self.writer('empty'); empty.finish()
        attrs = self.api.run('empty').load_full_data()
        self.assertEqual(attrs['historyKeys'], {'keys': {}, 'lastStep': -1})
        self.assertEqual(attrs['historyLineCount'], 0)
        self.assertEqual(attrs['systemMetrics'], {})
        writer = self.writer('rich')
        # Above the existing Table externalization threshold.
        table = backprop.Table(columns=['text'], data=[['x' * 150_000]])
        writer.log({'table': table})
        writer.finish()
        with patch.object(self.sink, 'download_file', side_effect=AssertionError('no blob reads')):
            reader = self.api.run('rich')
            rich = reader.load_full_data()['historyKeys']['keys']['table']['previousValue']
            self.assertEqual(rich['_type'], 'table')
            self.assertTrue(rich['external'])
            self.assertIn('sha256', rich)

    def test_lazy_full_load_refreshes_every_call_while_full_mode_respects_force(self):
        writer = self.writer(config={'value': 1}); writer.flush()
        full = self.api.run('run')
        lazy = list(self.api.runs())[0]
        writer.config.update({'value': 2}, allow_val_change=True); writer.flush()
        with patch.object(self.sink, 'read_events', wraps=self.sink.read_events) as read:
            self.assertEqual(full.load_full_data()['config']['value'], 1)
            self.assertEqual(read.call_count, 0)
            self.assertEqual(lazy.load()['config']['value'], 1)
            self.assertEqual(read.call_count, 0)
            self.assertEqual(lazy.load_full_data()['config']['value'], 2)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(lazy.load_full_data()['config']['value'], 2)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(full.load_full_data(force=True)['config']['value'], 2)
            self.assertEqual(read.call_count, 3)

    def test_upgrade_preserves_lazy_fetch_timing_and_refreshes_cached_objects_once(self):
        writer = self.writer(config={'value': 1}); writer.flush()
        with patch.object(self.sink, 'read_events', wraps=self.sink.read_events) as read:
            full = self.api.runs(lazy=False)
            self.assertEqual(read.call_count, 0)
            run = list(full)[0]
            self.assertEqual(read.call_count, 1)
            self.assertFalse(run._lazy)
            full.upgrade_to_full()
            self.assertEqual(read.call_count, 1)
        self.api.flush()
        untouched = self.api.runs()
        with patch.object(self.sink, 'read_events', wraps=self.sink.read_events) as read:
            untouched.upgrade_to_full()
            self.assertEqual(read.call_count, 0)
            self.assertFalse(list(untouched)[0]._lazy)
            self.assertEqual(read.call_count, 1)
        self.api.flush()
        collection = self.api.runs(); listed = list(collection)[0]
        writer.config.update({'value': 2}, allow_val_change=True); writer.flush()
        with patch.object(self.sink, 'read_events', wraps=self.sink.read_events) as read:
            self.assertIs(self.api.runs(lazy=False), collection)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(listed.config['value'], 2)
            collection.upgrade_to_full()
            self.assertEqual(read.call_count, 1)
            self.assertTrue(listed._lazy)  # Pinned upgrade keeps existing Run mode.

    def test_cache_clear_does_not_invalidate_a_returned_unevaluated_collection(self):
        writer = self.writer(); writer.finish()
        for lazy in (False, True):
            returned = self.api.runs(lazy=lazy)
            self.api.flush()
            actual = list(returned)
            self.assertEqual([run.id for run in actual], ['run'])
            self.assertEqual(actual[0]._lazy, lazy)

    def test_failed_forced_refresh_retains_the_last_good_snapshot(self):
        writer = self.writer(config={'value': 1}); writer.finish()
        reader = list(self.api.runs())[0]
        before = reader.load()
        with patch.object(self.sink, 'read_events', side_effect=OSError('temporary read failure')):
            with self.assertRaisesRegex(OSError, 'temporary read failure'):
                reader.load_full_data()
        self.assertEqual(reader.load(), before)
        with patch.object(self.sink, 'read_events', return_value=[]):
            with self.assertRaises(RunNotFoundError): reader.load(force=True)
        self.assertEqual(reader.load(), before)


@unittest.skipUnless(all((ROOT / path).is_file() for path in HASHES), 'Pinned W&B0.30.0 source required')
class PinnedRunAttributeContracts(JournalFixture, unittest.TestCase):
    def test_official_verifier_and_export_consumer_expressions_accept_native_return(self):
        writer = self.writer()
        for i in range(1, 11): writer.log({'loss': 1.0 / i}, step=i)
        writer.log({'dict': {'val1': 1.0, 'val2': 2}}, step=11)
        writer.finish()
        reader = self.api.run('run')
        tree, path = pinned_tree('wandb/sdk/verify/verify.py')
        test = next(node.test for node in ast.walk(tree) if isinstance(node, ast.If)
                    and isinstance(node.test, ast.BoolOp) and node.lineno == 169)
        self.assertFalse(eval(compile(ast.Expression(test), path, 'eval'), {'logged': True, 'prev_run': reader}))
        tree, path = pinned_tree('tools/wandb_export_history.py')
        expression = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign) and node.lineno == 63)
        exported_keys = eval(compile(ast.Expression(expression), path, 'eval'), {'run': reader})
        self.assertIn('loss', exported_keys)
        self.assertIn('dict.val1', exported_keys)

    def test_source_full_attribute_conversion_matches_native_heavy_field_values(self):
        writer = self.writer(config={'lr': .01})
        writer.log({'loss': 3}); writer.summary['loss'] = 7
        writer._emit('system', {'step': 0, 'metrics': {'cpu': 12}}); writer.finish()
        actual = self.api.run('run').load_full_data()
        raw = {'config': json.dumps({key: {'value': value} for key, value in actual['rawconfig'].items()}),
               'summaryMetrics': '{"loss":7}', 'systemMetrics': '{"cpu":12}',
               'historyKeys': {'keys': {'loss': {'previousValue': 3}}, 'lastStep': 0},
               'state': 'finished', 'name': 'run', 'historyLineCount': 1}
        namespace = pinned_methods('Run', {'_load_from_attrs'})
        receiver = SimpleNamespace(_attrs=copy.deepcopy(raw), _state='running')
        with supplied_requests(): expected = namespace['_load_from_attrs'](receiver)
        for key in ('config', 'rawconfig', 'summaryMetrics', 'state', 'name', 'historyLineCount'):
            self.assertEqual(actual[key], expected[key], key)
        self.assertEqual(actual['systemMetrics']['cpu'], expected['systemMetrics']['cpu'])
        self.assertEqual(actual['historyKeys']['keys']['loss']['previousValue'], expected['historyKeys']['keys']['loss']['previousValue'])

    def test_pinned_upgrade_only_refreshes_existing_lazy_runs_once(self):
        source = pinned_methods('Runs', {'upgrade_to_full'})['upgrade_to_full']
        for populated in (False, True):
            outcomes = []
            for expected in (True, False):
                calls = []
                items = [SimpleNamespace(_lazy=lazy, load_full_data=lambda i=i: calls.append(i))
                         for i, lazy in enumerate((True, False, True))] if populated else []
                if expected:
                    collection = SimpleNamespace(_lazy=True, objects=items)
                    operation = lambda: source(collection)
                else:
                    collection = Runs(lambda: (_ for _ in ()).throw(AssertionError('must not fetch')))
                    collection._items = items if populated else None
                    operation = collection.upgrade_to_full
                operation()
                first = sorted(calls)
                operation()
                outcomes.append((collection._lazy, first, sorted(calls)))
            self.assertEqual(outcomes[0], outcomes[1])

    def test_pinned_load_full_data_refresh_decision_matches_native_all_modes(self):
        namespace = pinned_methods('Run', {'load_full_data'})
        for lazy in (False, True):
            for force in (False, True):
                calls = []
                receiver = SimpleNamespace(_lazy=lazy, _attrs={'snapshot': 'old'},
                                           _load_with_fragment=lambda *args, **kwargs: calls.append(kwargs['force']) or {'snapshot': 'new'})
                expected = namespace['load_full_data'](receiver, force=force)
                actual_calls = []
                native = SimpleNamespace(_lazy=lazy, load=lambda *, force: actual_calls.append(force) or {'snapshot': 'new' if force else 'old'})
                actual = PublicRun.load_full_data(native, force=force)
                self.assertEqual(actual, expected)
                self.assertEqual([flag for flag in actual_calls if flag], calls)


if __name__ == '__main__': unittest.main()
