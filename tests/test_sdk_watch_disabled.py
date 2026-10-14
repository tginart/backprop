"""Disabled observation must remain inert even when PyTorch is unavailable.

Architecture: hash-checked pinned NoopRun callable -> native disabled run ->
matching None return before and after finish, with import/persistence tripwires.
Main callers: core SDK CI, including environments without optional frameworks.
"""
import ast
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backprop


class DisabledWatchContracts(unittest.TestCase):
    def test_missing_torch_error_matches_pinned_optional_import_boundary(self):
        from types import SimpleNamespace
        from backprop.telemetry import ModelWatcher
        source = Path(__file__).resolve().parents[1] / '.reference/wandb/source/wandb/util.py'
        if not source.exists():self.skipTest('Pinned W&B source required')
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         '414dd36708ea72b8429074b556f61962c03ce06bb7268570467051405bcfcb27')
        node = next(n for n in ast.parse(source.read_bytes()).body if isinstance(n, ast.FunctionDef) and n.name == 'get_module')
        tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
        def absent(*args):raise ModuleNotFoundError("No module named 'torch'", name='torch')
        namespace = {'_not_importable': set(), 'import_module_lazy': absent,
                     'logger': SimpleNamespace(exception=lambda *args: None), 'wandb': backprop}
        exec(compile(ast.fix_missing_locations(tree), str(source), 'exec'), namespace)
        message = "wandb.watch only works with pytorch, couldn't import torch."
        with self.assertRaises(backprop.Error) as reference:
            namespace['get_module']('torch', required=message)
        watcher = ModelWatcher(SimpleNamespace())
        with patch('backprop.torch_watch.importlib.import_module', side_effect=absent):
            with self.assertRaises(backprop.Error) as native:watcher.watch(object())
        self.assertIs(type(native.exception), type(reference.exception))
        self.assertEqual(str(native.exception), str(reference.exception))
        broken = ModuleNotFoundError('Missing dependency of an installed torch', name='internal_dependency')
        with patch('backprop.torch_watch.importlib.import_module', side_effect=broken):
            with self.assertRaises(ModuleNotFoundError) as caught:watcher.watch(object())
        self.assertIs(caught.exception, broken)
        self.assertEqual(watcher.models, {})

    def test_disabled_watch_and_unwatch_match_pinned_noop_without_imports_or_events(self):
        source = Path(__file__).resolve().parents[1] / '.reference/wandb/source/wandb/sdk/lib/noop_run.py'
        if not source.exists():
            self.skipTest('Pinned W&B source required')
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         'c945a79ef2eca1eee95c70e2658ea612b774fa2ea5e8b515049be6e8a5ac0d8f')
        tree = ast.parse(source.read_bytes())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'NoopRun')
        replacement = next(n for n in cls.body if isinstance(n, ast.For))
        self.assertIn('watch', ast.literal_eval(replacement.iter))
        self.assertIn('unwatch', ast.literal_eval(replacement.iter))
        expression = next(n.value for n in replacement.body if isinstance(n, ast.Assign))
        reference = eval(compile(ast.fix_missing_locations(ast.Expression(expression)), str(source), 'eval'))
        with tempfile.TemporaryDirectory() as directory:
            sink = backprop.MemorySink()
            run = backprop.Run(mode='disabled', dir=directory, _sink=sink,
                               settings={'silent': True, 'console': 'off', 'x_disable_stats': True})
            self.addCleanup(run.finish)
            for finished in [False, True]:
                if finished:
                    run.finish()
                with patch('backprop.torch_watch.importlib.import_module', side_effect=AssertionError('disabled import')), patch.object(backprop, 'termwarn') as warning:
                    for model in [None, object(), [], [object()]]:
                        options = dict(criterion=object(), log='invalid', log_freq='invalid', idx=object(), log_graph=True)
                        self.assertIs(run.watch(model, **options), reference(model, **options))
                        self.assertIs(run.unwatch(model), reference(model))
                    warning.assert_not_called()
            self.assertEqual(sink.events, [])
            self.assertEqual(run._watcher.models, {})


if __name__ == '__main__':
    unittest.main()
