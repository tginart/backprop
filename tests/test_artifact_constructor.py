"""Pinned constructor behavior and durable descriptive metadata preservation.

Architecture: isolated real reference/native constructors -> comparison at every
public alias -> real journal retry and independent reads. Main callers: SDK CI.
Storage-region selection remains an explicit unclosed contract, never normalized
out of a green class verdict. These tests certify the listed portable inputs.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from test_artifact_identity import SETTINGS

ROOT = Path(__file__).resolve().parents[1]


def compare(test, flags=(), label='portable'):
    """Run both implementations separately and retain raw CI observations.

    Main callers: portable and optional-framework test cases. Only terminal
    warning sinks are patched in the child; constructors execute unchanged.
    """
    try:
        reference = importlib.metadata.distribution('wandb')
    except importlib.metadata.PackageNotFoundError:
        test.skipTest('Pinned W&B wheel is unavailable')
    test.assertEqual(reference.version, '0.30.0')
    observations = []
    for mode in ['reference', 'native']:
        result = subprocess.run([sys.executable, '-B', str(ROOT/'tests/artifact_constructor_reference_probe.py'), mode, *flags],
            cwd=tempfile.gettempdir(), text=True, capture_output=True, timeout=90,
            env={**os.environ, 'PYTHONPATH':str(reference.locate_file('')) if mode == 'reference' else str(ROOT/'sdk')})
        test.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        observations.append(record)
        if os.environ.get('CI'):
            folder = ROOT/'test-results'
            folder.mkdir(exist_ok=True)
            (folder/f'artifact-constructor-{label}-{mode}.json').write_text(result.stdout)
    reference, native = observations
    for module, cases in reference['imports'].items():
        test.assertEqual(set(cases), set(native['imports'][module]))
        for name, expected in cases.items():
            with test.subTest(module=module, case=name):
                test.assertEqual(native['imports'][module][name], expected)
    return native


class ArtifactConstructorContracts(unittest.TestCase):
    def test_pinned_inputs_errors_warnings_and_imports(self):
        observed = compare(self)['imports']['wandb']
        # Independent useful-behavior assertions avoid treating comparison alone
        # as proof that a dictionary or descriptive value survived conversion.
        self.assertEqual(observed['metadata_tag_like']['value']['metadata'], {
            'number':{'_type':'number', 'value':'Infinity'},
            'timestamp':{'_type':'timestamp', 'value':12}})
        self.assertEqual(observed['metadata_dataclass']['value']['metadata'], {'a':{'count':2, 'when':'2026-01-02'}})
        self.assertEqual(observed['metadata_enum']['value']['metadata'], {'a':'FIRST'})
        self.assertEqual(observed['metadata_keys']['value']['metadata'], {'1':'int', 'null':'none', 'false':'false', '2.5':'float'})
        self.assertEqual(observed['name_colon']['error'], 'ValueError')
        self.assertEqual(observed['metadata_inf']['error'], 'ValueError')

    def test_detached_inputs_and_existing_collection_names(self):
        original = {'nested':[{'value':1}]}
        artifact = w.Artifact('dataset', 'data', metadata=original)
        original['nested'][0]['value'] = 9
        self.assertEqual(artifact.metadata, {'nested':[{'value':1}]})
        artifact.name = 'renamed collection'
        self.assertEqual(artifact.name, 'renamed collection')
        # Public creation is strict, but restored names can originate from a
        # later collection edit. Loading must not reapply creation-only rules.
        restored = w.Artifact._new_internal('renamed collection', 'run_table')
        self.assertEqual(restored.name, 'renamed collection')
        with self.assertRaises(ValueError):
            w.Artifact('renamed collection', 'data')

    def test_metadata_retry_restart_edits_and_duplicate_reconstruction(self):
        from artifact_constructor_replay_fixture import build
        result = build()
        self.assertEqual(result['python_metadata'], result['expected'])
        self.assertTrue(result['immutable_contents_verified'])
        self.assertTrue(result['stable_version'])
        self.assertTrue(result['failed_edit_left_snapshot_unchanged'])

    def test_region_remains_an_explicit_gap(self):
        with self.assertRaisesRegex(NotImplementedError, 'Foundry file dataset'):
            w.Artifact('dataset', 'data', storage_region='coreweave-us')
