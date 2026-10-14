"""Artifact descriptive values from real optional training frameworks.

Architecture: installed framework values -> isolated constructor comparison at
three public imports. Main callers: framework CI environments and local SDK QA.
No GPU is needed; this does not certify CUDA or multi-device logging behavior.
"""
import importlib.util
import unittest
from test_artifact_constructor import compare


class FrameworkArtifactInputs(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('numpy'), 'NumPy is unavailable')
    def test_numpy(self):
        compare(self, ['--numpy'], 'numpy')

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'PyTorch is unavailable')
    def test_torch(self):
        compare(self, ['--torch'], 'torch')

    @unittest.skipUnless(importlib.util.find_spec('tensorflow'), 'TensorFlow is unavailable')
    def test_tensorflow(self):
        compare(self, ['--tensorflow'], 'tensorflow')

    @unittest.skipUnless(importlib.util.find_spec('jax'), 'JAX is unavailable')
    def test_jax(self):
        compare(self, ['--jax'], 'jax')

    @unittest.skipUnless(importlib.util.find_spec('xxhash'), 'xxHash is unavailable')
    def test_xxhash(self):
        compare(self, ['--xxhash'], 'xxhash')
