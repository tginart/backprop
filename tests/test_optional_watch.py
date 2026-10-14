"""Exercise complete watch registration and removal lifecycles with real Torch.

Architecture: independent pinned-source/native processes compare registrations,
frequency, warnings and errors -> native run tests verify durable observations,
rollback and ownership. Existing graph/CUDA suites cover graph bytes and devices.
Main callers: local SDK checks and the optional lower/current Torch CI jobs.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec('torch'), 'Optional PyTorch required')
class WatchLifecycleContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.store = backprop.MemorySink()
        self.run = backprop.init(project='watch', entity='e', dir=self.directory.name, _sink=self.store,
                                 settings={'console': 'off', 'silent': True, 'x_disable_stats': True},
                                 reinit='create_new')
        self.addCleanup(self.run.finish)

    def test_pinned_registrations_warnings_and_errors_in_separate_processes(self):
        if not (ROOT / '.reference/wandb/source').exists():
            self.skipTest('Pinned W&B source required')
        results = []
        for kind in ['reference', 'native']:
            output = subprocess.run([sys.executable, '-B', str(ROOT / 'tests/watch_reference_probe.py'), kind],
                                    env={**os.environ, 'PYTHONPATH': str(ROOT / 'sdk')},
                                    text=True, capture_output=True, timeout=60)
            self.assertEqual(output.returncode, 0, output.stderr)
            results.append(json.loads(output.stdout))
        for scenario in results[0]:
            with self.subTest(scenario=scenario):
                self.assertEqual(results[0][scenario], results[1][scenario])

    def test_repeated_observations_persist_and_all_owned_hooks_are_removed(self):
        import torch
        model = torch.nn.Linear(2, 1)
        for _ in range(2):
            self.assertIsNone(self.run.watch(model, log='parameters', log_freq=1, idx=0))
        self.assertIsNone(self.run.watch(model, log='gradients', log_freq=1, idx=2))
        model(torch.ones(1, 2)).sum().backward(); self.run.log({'loss': 1})
        self.assertIsNone(self.run.unwatch([]))
        self.assertFalse(model._forward_hooks)
        self.assertTrue(all(not p._backward_hooks for p in model.parameters()))
        self.assertEqual(self.run._watcher._gradient_handles, {})
        model(torch.ones(1, 2)).sum().backward(); self.run.log({'loss': 2})
        self.run.finish()
        rows = list(Api._from_store(self.store, entity='e', project='watch').run(self.run.id).scan_history())
        self.assertIn('parameters/weight', rows[0]); self.assertIn('gradients/graph_2weight', rows[0])
        self.assertNotIn('parameters/weight', rows[1]); self.assertNotIn('gradients/graph_2weight', rows[1])

    def test_failed_repeat_rolls_back_only_new_handles_and_preserves_existing_graph(self):
        import torch
        model = torch.nn.Sequential(torch.nn.Linear(2, 1))
        self.run.watch(model, log='all', log_graph=True, idx=0, log_freq=1)
        graph = self.run._watcher.graphs[id(model)]
        forward_count = sum(len(m._forward_hooks) for m in model.modules())
        gradient_count = sum(len(p._backward_hooks or {}) for p in model.parameters())
        for index in [0, 1]:
            with self.assertRaises(ValueError):
                self.run.watch(model, log='all', log_graph=True, idx=index)
            self.assertIs(self.run._watcher.graphs[id(model)], graph)
            self.assertEqual(sum(len(m._forward_hooks) for m in model.modules()), forward_count)
            self.assertEqual(sum(len(p._backward_hooks or {}) for p in model.parameters()), gradient_count)
        model(torch.ones(1, 2)).sum().backward(); self.run.log({'loss': 1})
        self.assertTrue(graph.loaded)
        self.run.unwatch(model)
        self.run.watch(model, log_graph=True)
        self.run.finish()
        self.assertTrue(all(not m._forward_hooks for m in model.modules()))
        self.assertTrue(all(not p._backward_hooks for p in model.parameters()))

    def test_concurrent_module_replicas_preserve_logical_graph_identity_and_parameters(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        import torch
        model = torch.nn.Sequential(torch.nn.Linear(2, 3), torch.nn.ReLU(), torch.nn.Linear(3, 1))
        self.run.watch(model, log=None, log_graph=True)
        graph = self.run._watcher.graphs[id(model)]
        originals = list(model.modules())
        def clone_model():
            # Exercise PyTorch's real module replication operation and its
            # former-parameter layout, without pretending to own a second GPU.
            copies = {id(original): original._replicate_for_data_parallel() for original in originals}
            for original in originals:
                replica = copies[id(original)]
                replica._former_parameters = {}
                for name, child in original._modules.items():
                    replica._modules[name] = copies[id(child)] if child is not None else None
                for name, parameter in original._parameters.items():
                    if parameter is not None:
                        value = parameter.detach().clone().requires_grad_(parameter.requires_grad)
                        setattr(replica, name, value)
                        replica._former_parameters[name] = value
            return copies[id(model)]
        replicas = [clone_model(), clone_model()]
        self.assertTrue(all(not list(replica.parameters()) for replica in replicas))
        ready = Barrier(2)
        def forward(replica):
            ready.wait(timeout=5)
            return replica(torch.ones(4, 2))
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(forward, replicas))
        self.assertEqual([list(value.shape) for value in outputs], [[4, 1], [4, 1]])
        self.assertTrue(graph.loaded)
        self.assertEqual(len(graph.nodes), 3)
        self.assertEqual({node.name: sum(node.num_parameters) for node in graph.nodes}, {'0': 9, '1': 0, '2': 4})
        self.assertEqual(graph.metadata['capture'], {'complete': True, 'missing_modules': []})
        self.run.finish()
        public = Api._from_store(self.store, entity='e', project='watch').run(self.run.id)
        descriptor = public.summary['graph_0']
        saved = json.loads(self.store.download_file(descriptor))
        self.assertEqual(len(saved['nodes']), 3)
        self.assertTrue(descriptor['capture']['complete'])

    def test_late_graph_callback_cannot_mutate_a_finished_run(self):
        import torch
        model = torch.nn.Sequential(torch.nn.Linear(2, 1))
        self.run.watch(model, log=None, log_graph=True)
        graph = self.run._watcher.graphs[id(model)]
        # PyTorch may have copied a callback before finish removed its handle.
        callback = next(iter(model[0]._forward_hooks.values()))
        self.run.finish()
        before = len(self.store.events)
        callback(model[0], (torch.ones(1, 2),), torch.ones(1, 1))
        self.assertFalse(graph.loaded)
        self.assertEqual(graph.nodes, [])
        self.assertEqual(graph.metadata['capture'], {'complete': False, 'missing_modules': ['0']})
        self.assertEqual(len(self.store.events), before)
        public = Api._from_store(self.store, entity='e', project='watch').run(self.run.id)
        self.assertFalse(public.summary['graph_0']['capture']['complete'])

    def test_removed_gradient_handles_allow_name_reuse_without_corrupting_other_models(self):
        import torch
        first, second = torch.nn.Linear(2, 1), torch.nn.Linear(2, 1)
        self.run.watch(first, idx=0)
        for handle in self.run._watcher.models[id(first)].handles:
            handle.remove()
        self.run.watch(second, idx=0)
        self.run.unwatch(first)
        self.assertEqual(len(self.run._watcher._gradient_handles), 2)
        with self.assertRaises(ValueError):
            self.run.watch(first, idx=0)
        self.run.unwatch(second)
        self.assertEqual(self.run._watcher._gradient_handles, {})

    def test_two_ddp_workers_match_pinned_measurements_and_reconstruct_shared_history(self):
        import base64
        import copy
        import torch
        from backprop.apis.public.replay import reduce_events
        if not torch.distributed.is_available() or not torch.distributed.is_gloo_available():
            self.skipTest('Two-process CPU verification requires the existing Gloo runtime')
        if not (ROOT / '.reference/wandb/source').exists():
            self.skipTest('Pinned W&B source required')
        directory = Path(self.directory.name)
        processes = []
        try:
            for rank in range(2):
                process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'tests/watch_distributed_probe.py'),
                                            str(rank), str(directory)],
                                           env={**os.environ, 'PYTHONPATH': str(ROOT / 'sdk')},
                                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
            for process in processes:
                out, error = process.communicate(timeout=45)
                self.assertEqual(process.returncode, 0, out + error)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.communicate()
        combined = backprop.MemorySink()
        results = [json.loads((directory / f'result-{rank}.json').read_text()) for rank in range(2)]
        for result in results:
            combined.publish(result['events'])
            combined.files.update({name: base64.b64decode(data) for name, data in result['files'].items()})
        public = Api._from_store(combined, entity='e', project='watch').run('distributed-watch')
        rows = list(public.scan_history())
        self.assertEqual(public.state, 'finished')
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row['_writer'] for row in rows}), 2)
        for rank in range(2):
            writer_rows = sorted((row for row in rows if row['rank'] == rank), key=lambda row: row['_step'])
            self.assertEqual([row['_step'] for row in writer_rows], [0, 1])
            self.assertEqual([row['epoch'] for row in writer_rows], [0, 1])
            for row, expected in zip(writer_rows, results[rank]['expected']):
                actual = {key: value for key, value in row.items() if key.startswith(('gradients/', 'parameters/'))}
                self.assertEqual(actual, expected)
        self.assertEqual(reduce_events(combined.events),
                         reduce_events(list(reversed(combined.events)) + copy.deepcopy(combined.events)))

    @unittest.skipUnless(importlib.util.find_spec('lightning'), 'Optional Lightning Fabric required')
    def test_fabric_watch_full_signature_matches_pinned_forwarding_and_errors(self):
        import ast
        import hashlib
        import inspect
        from types import SimpleNamespace
        from unittest.mock import Mock
        from backprop.integration.lightning.fabric import WandbLogger
        from backprop.integration.lightning.fabric.logger import WandbLogger as ModuleLogger
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb.integration.lightning.fabric import WandbLogger as PublicLogger
        self.assertIs(WandbLogger, ModuleLogger); self.assertIs(PublicLogger, ModuleLogger)
        source = ROOT / '.reference/wandb/source/wandb/integration/lightning/fabric/logger.py'
        data = source.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         '7d8f86bf6f31356492ab80239fffcc90dbed59d56cb0e77d9ffb8ce60c2ddc57')
        cls = next(node for node in ast.parse(data).body if isinstance(node, ast.ClassDef) and node.name == 'WandbLogger')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'watch')
        tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
        namespace = {}
        exec(compile(ast.fix_missing_locations(tree), str(source), 'exec'), namespace)
        reference = namespace['watch']
        self.assertEqual(inspect.signature(reference), inspect.signature(WandbLogger.watch))
        model = object()
        for options in [{}, *[{'log': log, 'log_freq': frequency, 'log_graph': graph}
                              for log in [None, 'gradients', 'parameters', 'all']
                              for frequency in [1, 100] for graph in [False, True]]]:
            outcomes = []
            for function in [reference, WandbLogger.watch]:
                target = Mock(return_value=object())
                self.assertIsNone(function(SimpleNamespace(experiment=SimpleNamespace(watch=target)), model, **options))
                outcomes.append(target.call_args)
            self.assertEqual(outcomes[0], outcomes[1])
        for function in [reference, WandbLogger.watch]:
            error = ValueError('downstream validation')
            target = Mock(side_effect=error)
            receiver = SimpleNamespace(experiment=SimpleNamespace(watch=target))
            with self.assertRaises(ValueError) as caught:
                function(receiver, model)
            self.assertIs(caught.exception, error)
            with self.assertRaises(TypeError):function(receiver, model, unknown=True)

    @unittest.skipUnless(importlib.util.find_spec('lightning'), 'Optional Lightning Fabric required')
    def test_fabric_watch_logs_real_graphs_and_suppresses_nonzero_rank(self):
        import torch
        from lightning.fabric.utilities.rank_zero import rank_zero_only
        from backprop.integration.lightning.fabric import WandbLogger
        logger = WandbLogger(experiment=self.run, project='watch')
        with patch.object(rank_zero_only, 'rank', 0):
            model = torch.nn.Sequential(torch.nn.Linear(2, 1))
            self.assertIsNone(logger.watch(model, log='all', log_freq=1, log_graph=True))
            self.assertIsNone(logger.watch(model, log='parameters', log_freq=1, log_graph=False))
            model(torch.ones(1, 2)).sum().backward(); self.run.log({'loss': 1})
            graph = self.run._watcher.graphs[id(model)]
            self.assertTrue(graph.loaded)
        count = len(self.run._watcher.models)
        with patch.object(rank_zero_only, 'rank', 1):
            self.assertIsNone(logger.watch(torch.nn.Linear(2, 1)))
        self.assertEqual(len(self.run._watcher.models), count)
        self.run.finish()
        public = Api._from_store(self.store, entity='e', project='watch').run(self.run.id)
        self.assertIn('parameters/graph_10.weight', public.summary)
        self.assertTrue(public.summary['graph_0']['capture']['complete'])

    def test_imported_top_level_aliases_keep_none_return_and_model_warning(self):
        import torch
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb import watch, unwatch
        model = torch.nn.Linear(2, 1)
        with patch.object(backprop, 'run', self.run):
            self.assertIsNone(watch(model, None, 'gradients', 1, 0, False))
            self.assertIsNone(watch(model, None, 'parameters', 1, 1, False))
            self.assertIsNone(unwatch(model))
            with patch.object(backprop, 'termwarn') as warning:
                self.assertIsNone(unwatch(model))
                warning.assert_called_once_with(f'{model} model has not been watched')


if __name__ == '__main__':
    unittest.main()
