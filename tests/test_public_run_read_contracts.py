"""Compare public read contracts with unchanged pinned methods at the service boundary.

Architecture: real SDK journals -> native public snapshot -> unchanged pinned
getter/refresh methods with decoded service attributes -> observable comparison.
Main callers: combined SDK CI. Foundry snapshot fields replace backend-specific
GraphQL records; these checks do not assert W&B account records, GPU collection,
or undocumented server history-index metadata.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
import subprocess
import sys
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_public_api_attributes import HASHES, ROOT, JournalFixture, pinned_methods


@unittest.skipUnless(all((ROOT / name).is_file() for name in HASHES),
                     'Pinned W&B0.30.0 source required')
class PublicReadContracts(JournalFixture, unittest.TestCase):
    def test_system_getter_matches_actual_pinned_decoding_with_sparse_cpu_gpu_keys(self):
        writer = self.writer()
        writer._emit('system', {'step': 0, 'metrics': {'cpu': 12, 'gpu.0.memory': 128,
                                                     'disk.used': 1024, 'absent': None}})
        writer.finish()
        native = self.api.run('run')
        # The getter reads values that already exist. GPU collection remains a
        # separately verified logging concern; this fixture claims no GPU run.
        actual = native.system_metrics
        self.assertEqual(actual['cpu'], 12)
        self.assertEqual(actual['gpu.0.memory'], 128)
        getter = pinned_methods('Run', {'system_metrics'})['system_metrics'].fget
        for encoded in (copy.deepcopy(actual), json.dumps(actual),
                        json.dumps(actual).encode(), bytearray(json.dumps(actual).encode())):
            receiver = SimpleNamespace(_lazy=False, _full_data_loaded=True,
                                       _attrs={'systemMetrics': encoded})
            self.assertEqual(getter(receiver), actual)
        with patch.object(self.sink, 'read_events', side_effect=AssertionError('Cached getter fetched events')):
            self.assertEqual(native.system_metrics, actual)
        actual['cpu'] = -1
        self.assertEqual(native.system_metrics['cpu'], 12)

    def test_empty_system_getter_and_automatic_lazy_load_have_explicit_boundaries(self):
        getter = pinned_methods('Run', {'system_metrics'})['system_metrics'].fget
        for lazy, loaded, supplied in ((False, False, False), (True, True, False),
                                       (True, False, True), (True, False, False)):
            calls = []
            receiver = SimpleNamespace(_lazy=lazy, _full_data_loaded=loaded,
                                       _attrs={'systemMetrics': {}} if supplied else {})
            def load():
                calls.append('full')
                receiver._attrs['systemMetrics'] = {'cpu': 7}
            receiver.load_full_data = load
            expected = getter(receiver)
            should_fetch = lazy and not loaded and not supplied
            self.assertEqual(calls, ['full'] if should_fetch else [])
            self.assertEqual(expected, {'cpu': 7} if should_fetch else {})
        writer = self.writer()
        writer.finish()
        native = list(self.api.runs(lazy=True))[0]
        # Foundry listing reads a complete event snapshot, so the value is
        # already available even on a lazy handle. It does not issue GraphQL.
        with patch.object(self.sink, 'read_events', side_effect=AssertionError('Already loaded snapshot fetched again')):
            self.assertEqual(native.system_metrics, {})

    def test_external_system_descriptor_matches_pinned_without_blob_download(self):
        """Read actual external event evidence without hydrating its file.

        Main caller: system_metrics public-contract verification. Why this
        exists: the pinned getter decodes only the enclosing JSON dictionary;
        opening a descriptor changes the returned value and adds a hidden read.
        The Table is genuinely externalized/uploaded by the logging SDK first.
        """
        import backprop
        writer = self.writer()
        writer.log({'table': backprop.Table(columns=['text'], data=[['x' * 150_000]])})
        writer.flush()
        descriptor = copy.deepcopy(next(event for event in self.sink.events
                                        if event['type'] == 'history')['payload']['metrics']['table'])
        self.assertTrue(descriptor['external'])
        writer._emit('system', {'step': 0, 'metrics': {'rich': descriptor}})
        writer.finish()
        getter = pinned_methods('Run', {'system_metrics'})['system_metrics'].fget
        with patch.object(self.sink, 'download_file', side_effect=AssertionError('System getter opened a blob')):
            reader = self.api.run('run')
            # Include the actual event snapshot's step/runtime envelope. Its
            # projected descriptor is independently checked against the saved
            # event above, rather than using the getter output as its oracle.
            fields = reader.load()['systemMetrics']
            self.assertEqual(fields['rich'], descriptor)
            receiver = SimpleNamespace(_lazy=False, _full_data_loaded=True,
                                       _attrs={'systemMetrics': copy.deepcopy(fields)})
            expected = getter(receiver)
            actual = reader.system_metrics
        self.assertEqual(actual, expected)
        self.assertEqual(actual['rich'], descriptor)

    def test_system_getter_detached_copy_is_an_explicit_native_repair(self):
        """Preserve both outcomes for cached-dictionary mutation semantics.

        Main caller: public getter support evidence. The pinned cached dict is
        mutable through the returned value. Native snapshot reads intentionally
        detach that value; record the difference instead of normalizing it away.
        """
        writer = self.writer()
        writer._emit('system', {'step': 0, 'metrics': {'cpu': 12}})
        writer.finish()
        reader = self.api.run('run')
        getter = pinned_methods('Run', {'system_metrics'})['system_metrics'].fget
        receiver = SimpleNamespace(_lazy=False, _full_data_loaded=True,
                                   _attrs={'systemMetrics': copy.deepcopy(reader.load()['systemMetrics'])})
        reference = getter(receiver)
        native = reader.system_metrics
        self.assertEqual(reference, native)
        reference['cpu'] = native['cpu'] = -1
        self.assertIs(getter(receiver), reference)
        self.assertEqual(getter(receiver)['cpu'], -1)
        self.assertEqual(reader.system_metrics['cpu'], 12)

    def test_load_selects_the_pinned_refresh_policy_for_each_force_and_lazy_mode(self):
        source = pinned_methods('Run', {'load', '_load_from_attrs'})
        writer = self.writer(config={'value': 1})
        writer.log({'loss': 2})
        writer.finish()
        for lazy in (False, True):
            for force in (False, True):
                reader = self.api.run('run')
                reader._lazy = lazy
                previous = reader.load()
                calls = []
                receiver = SimpleNamespace(_lazy=lazy, _state=previous["state"], _attrs=copy.deepcopy(previous))
                receiver._load_from_attrs = lambda: source['_load_from_attrs'](receiver)
                def fetch(fragment, name, requested):
                    calls.append((fragment, name, requested))
                    return receiver._attrs
                receiver._load_with_fragment = fetch
                reference = source['load'](receiver, force=force)
                with patch.object(self.sink, 'read_events', wraps=self.sink.read_events) as read:
                    actual = reader.load(force=force)
                self.assertEqual(read.call_count, int(force))
                self.assertEqual(calls, [('light' if lazy else 'full', 'light' if lazy else 'full', force)])
                for key in ('config', 'summaryMetrics', 'systemMetrics', 'historyKeys',
                            'historyLineCount', 'state', 'name', 'displayName'):
                    self.assertEqual(actual[key], reference[key], (lazy, force, key))

    def test_public_import_aliases_share_the_verified_methods(self):
        code = """
import importlib
from backprop.apis.public.runs import Run
from backprop.compatibility import install_wandb_aliases
install_wandb_aliases()
for name in ('backprop.apis.public', 'backprop.apis.public.runs',
             'wandb.apis.public', 'wandb.apis.public.runs'):
    assert importlib.import_module(name).Run is Run, name
    for method in ('load', 'load_full_data', 'system_metrics'):
        assert getattr(importlib.import_module(name).Run, method) is getattr(Run, method)
"""
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, '-B', '-c', code], cwd=self.temp.name,
                                env={**os.environ, 'PYTHONPATH': str(root/'sdk')},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
