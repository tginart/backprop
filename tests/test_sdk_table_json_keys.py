"""Check pinned JSON key behavior across Table logging and resumed mutation.

Architecture: hash-checked pinned dictionary traversal plus real json.dump rules
-> native Table encoding -> actual offline sync/Public Api/artifact readback.
The reference isolates unrelated scalar/media conversion; dictionary traversal,
sorted key validation and JSON conversion run unchanged. Tests also ensure the
original Python constraints survive resume without changing public file data.
Main callers: unittest discovery; no optional dependencies or network required.
"""
import ast
import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import backprop
from backprop.data_types import Encoder
from backprop.json_keys import resume_schemas, table_schema_fields
from backprop.table_types import ConstType
from backprop.transport import sync


SOURCE = Path(__file__).resolve().parents[1] / '.reference/wandb/source/wandb/sdk/data_types/utils.py'


def reference_json(value):
    data = SOURCE.read_bytes()
    if hashlib.sha256(data).hexdigest() != 'fcc0cbc242da356d725d9daf9900330df92717036dcf3d4ff84ffd17116b1dc7':
        raise AssertionError('Unexpected pinned Table JSON helper')
    node = next(node for node in ast.parse(data).body if isinstance(node, ast.FunctionDef) and node.name == '_json_helper')
    ns = {'WBValue': type('WBValue', (), {}), 'datetime': datetime, 'Decimal': Decimal,
          'util': SimpleNamespace(json_friendly=lambda item: (item, True))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), ns)
    return json.loads(json.dumps(ns['_json_helper'](value, None), sort_keys=True))


class TableJsonKeyTests(unittest.TestCase):
    def encode(self, value):
        return Encoder(lambda *args, **kwargs: self.fail('No files expected'), context='table').encode(value)

    @unittest.skipUnless(SOURCE.exists(), 'Pinned W&B reference required')
    def test_json_scalar_key_normalization_matches_pinned(self):
        cases = [{1: 'one'}, {True: 'yes'}, {None: 'null'}, {1.5: 'fraction'},
                 {float('inf'): 'infinity'}, {float('nan'): 'nan'},
                 {2: 'two', 1.5: 'fraction'}, {'nested': [{7: 'seven'}]}]
        for value in cases:
            with self.subTest(value=value):
                before = list(value.items())
                self.assertEqual(self.encode(value), reference_json(value))
                self.assertEqual(list(value.items()), before)

    @unittest.skipUnless(SOURCE.exists(), 'Pinned W&B reference required')
    def test_incomparable_and_non_json_keys_fail_like_sorted_pinned_dump(self):
        for value in ({1: 'int', '1': 'string'}, {None: 'null', 1: 'int'},
                      {(1, 2): 'tuple'}, {object(): 'object'}, {1j: 'complex'}):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    reference_json(value)
                with self.assertRaises(TypeError):
                    self.encode(value)

    def test_dictionary_and_constant_schemas_normalize_without_mutating_inputs(self):
        declarations = [{1: int}, ConstType({2: 'two'}), ConstType(({3: 'three'},))]
        values = [{1: 7}, {2: 'two'}, ({3: 'three'},)]
        for dtype, value in zip(declarations, values):
            with self.subTest(dtype=dtype):
                table = backprop.Table(columns=['value'], dtype=dtype, data=[[value]], optional=False)
                original = table._types[0].to_json()
                fields = table_schema_fields([original], for_resume=True)
                self.assertIn('resume_column_types', fields)
                self.assertEqual(resume_schemas(json.loads(json.dumps(fields))), [original])
                self.assertEqual(table._types[0].to_json(), original)
                artifact = backprop.Artifact('json-keys', 'dataset')
                entry = artifact.add(table, 'table')
                raw = json.loads(Path(entry.local_path).read_text())
                self.assertNotIn('resume_column_types', raw)
                restored = artifact.get('table')
                wanted = ({'3': 'three'},) if isinstance(value, tuple) else {str(key): item for key, item in value.items()}
                self.assertEqual(restored.data, [[wanted]])
                restored.add_data(wanted)
                table.add_data(value)
                self.assertEqual(table.data, [[value], [value]])

    def test_boolean_null_and_fractional_schema_keys_roundtrip(self):
        for key in (True, None, 1.5, float('inf')):
            with self.subTest(key=key):
                table = backprop.Table(columns=['value'], dtype={key: int}, data=[[{key: 3}]], optional=False)
                artifact = backprop.Artifact('typed-json-keys', 'dataset')
                artifact.add(table, 'table')
                restored = artifact.get('table')
                wanted = json.loads(json.dumps({key: 3}))
                self.assertEqual(restored.data, [[wanted]])
                restored.add_data(wanted)

    def test_normal_string_schemas_need_no_resume_copy_and_bad_copy_fails(self):
        table = backprop.Table(columns=['value'], data=[[{'x': 7}]], dtype={'x': int})
        fields = table_schema_fields([table._types[0].to_json()], for_resume=True)
        self.assertNotIn('resume_column_types', fields)
        for invalid in (7, {'kind': 'list', 'items': [3]}, {'kind': 'module', 'items': []}):
            with self.subTest(value=invalid), self.assertRaises((TypeError, ValueError)):
                resume_schemas({'resume_column_types': invalid})

    def test_failed_table_logging_leaves_pending_row_and_artifacts_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            run = backprop.Run(project='json-keys', dir=directory, mode='offline',
                               settings={'silent': True, 'console': 'off', 'disable_git': True})
            try:
                run.log({'loss': 1}, commit=False)
                table = backprop.Table(columns=['value'], data=[[{1: 'int', '1': 'string'}]], allow_mixed_types=True)
                before = (run._journal.count, dict(run._pending), table._offset)
                with self.assertRaises(TypeError):
                    run.log({'table': table})
                self.assertEqual((run._journal.count, run._pending, table._offset), before)
                self.assertIsNone(table._snapshot)
                self.assertIsNone(table._artifact_target)
            finally:
                run.finish()

    def test_incremental_resume_keeps_python_keys_but_reads_normalized_data(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = backprop.MemorySink()
            settings = {'silent': True, 'console': 'off', 'disable_git': True}
            run = backprop.Run(project='json-keys', dir=directory, mode='offline', settings=settings)
            table = backprop.Table(columns=['value'], dtype={1: int}, data=[[{1: 2}]],
                                   optional=False, log_mode='INCREMENTAL')
            run.log({'table': table}); run.finish(); sync(run.sync_dir, sink=sink)
            resumed = backprop.Run(project='json-keys', id=run.id, resume='must', dir=directory,
                                   _sink=sink, _deployment={}, settings=settings)
            try:
                continuation = backprop.Table(columns=['value'], dtype={1: int}, data=[[{1: 3}]],
                                              optional=False, log_mode='INCREMENTAL')
                resumed.log({'table': continuation})
                continuation.add_data({1: 4})
                resumed.log({'table': continuation})
                before = continuation.data[:]
                with self.assertRaises(TypeError):
                    continuation.add_data({1: 'invalid'})
                self.assertEqual(continuation.data, before)
            finally:
                resumed.finish()
            reader = backprop.Api._from_store(sink, project='json-keys').run(run.id)
            rows = list(reader.scan_history())
            self.assertEqual([row['table']['data'] for row in rows], [[[{'1': 2}]], [[{'1': 3}]], [[{'1': 4}]]])
            artifact = list(reader.logged_artifacts())[-1]
            entry = next(item for item in artifact.manifest.entries.values() if item.path.endswith('.table.json'))
            self.assertEqual(artifact.get(entry.path).data, [[{'1': 2}], [{'1': 3}], [{'1': 4}]])


if __name__ == '__main__':
    unittest.main()
