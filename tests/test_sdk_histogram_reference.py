"""Compare Histogram behavior with the exact pinned upstream class body.

Architecture: parse the local W&B 0.30.0 source -> execute its unchanged class
with only the base/import boundary supplied -> compare results and failures.
Real NumPy computes bins in both paths. This checks the numerical wrapper, not
the upstream W&B service. No dependency installation or network is involved.
Main callers: unittest discovery with the pinned .reference checkout present.
"""
import ast
import hashlib
import importlib
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import patch

from backprop import Histogram, Error
from backprop.values import WBValue

SOURCE = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/sdk/data_types/histogram.py"
PINNED_SHA256 = "1a5a4e7a3bf3abf3f0fd55bba7111ca63e6c9c3558949b662ab82be44b389930"


def load_reference():
    """Load the unchanged upstream class while bypassing unrelated SDK imports.

    Main callers: reference test setup. The hash prevents a different local
    W&B version from silently becoming the claimed compatibility baseline.
    """
    source = SOURCE.read_bytes()
    if hashlib.sha256(source).hexdigest() != PINNED_SHA256:
        raise AssertionError("Histogram source no longer matches pinned W&B 0.30.0")
    parsed = ast.parse(source, filename=str(SOURCE))
    selected = [node for node in parsed.body if isinstance(node, ast.ImportFrom) and node.module == "__future__"
                or isinstance(node, ast.ClassDef) and node.name == "Histogram"]
    namespace = {"WBValue": WBValue, "util": SimpleNamespace(get_module=lambda name, **kwargs: importlib.import_module(name))}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["Histogram"]


@unittest.skipUnless(SOURCE.is_file() and importlib.util.find_spec("numpy"), "Pinned source and NumPy are required")
class HistogramReferenceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = load_reference()

    def outcome(self, factory, kwargs):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                result = factory(**kwargs)
            except Exception as error:
                outcome = ("error", type(error), str(error))
            else:
                outcome = ("result", result.to_json())
        return outcome, [(type(item.message), str(item.message)) for item in caught]

    def test_default_invalid_and_precomputed_input_parity(self):
        import numpy as np
        cases = [{}, {"sequence": None}, {"sequence": []}, {"sequence": [1, 2], "np_histogram": []},
                 {"sequence": [[1, 2], [3, 4]]}, {"sequence": [math.nan]}, {"sequence": [math.inf]},
                 {"sequence": [1, 2], "num_bins": 0}, {"sequence": [1], "num_bins": 513},
                 {"sequence": [1, 2], "num_bins": 2.5}, {"sequence": [1, 2], "num_bins": np.int64(2)},
                 {"np_histogram": ([1],)}, {"np_histogram": ([1], [0, 1], [2])},
                 {"np_histogram": ((1, 2), (0, 1, 2))}, {"np_histogram": ([], [0])},
                 {"np_histogram": ([-1], [1, 0])}, {"np_histogram": ([1, 2], [0, 1])},
                 {"np_histogram": ([1] * 513, list(range(514)))},
                 {"np_histogram": (np.array([2, 3]), np.array([0., 1., 2.]))}]
        for kwargs in cases:
            with self.subTest(kwargs=repr(kwargs)[:120]):
                self.assertEqual(self.outcome(Histogram, kwargs), self.outcome(self.reference, kwargs))

    def test_floating_boundaries_large_ranges_and_bin_strategies(self):
        import numpy as np
        rng = np.random.default_rng(914)
        cases = [(np.array([0., .1, .2, .3, 1.]), 10), ([-1e308, 1e308], 4),
                 ([1e308, 1e308], 4), ([1e-320, 2e-320], 64), ([-1e300, 1e300], 64)]
        for bins in [1, 3, 32, 512, "auto", "fd", np.array([-3, -1, 0, 1, 3])]:
            cases.append((rng.normal(size=300), bins))
        for scale in [1e-100, 1., 1e100]:
            edges = np.linspace(-scale, scale, 17)
            values = np.concatenate([edges, np.nextafter(edges, -np.inf), np.nextafter(edges, np.inf)])
            cases.append((values, 16))
        for values, bins in cases:
            with self.subTest(bins=repr(bins), sample=repr(values)[:80]):
                kwargs = {"sequence": values, "num_bins": bins}
                self.assertEqual(self.outcome(Histogram, kwargs), self.outcome(self.reference, kwargs))

    def test_precomputed_python_containers_keep_public_identity(self):
        for factory in (Histogram, self.reference):
            counts, edges = [1, 2], [0, 1, 2]
            value = factory(np_histogram=(counts, edges))
            self.assertIs(value.histogram, counts)
            self.assertIs(value.bins, edges)
            self.assertIs(value.to_json()["values"], counts)
            self.assertIs(value.to_json()["bins"], edges)
            counts[0] = 3
            self.assertEqual(value.to_json()["values"], [3, 2])
            tuples = factory(np_histogram=((1,), (0, 1)))
            self.assertIsInstance(tuples.histogram, tuple)
            self.assertIsInstance(tuples.bins, tuple)


class HistogramDependencyContracts(unittest.TestCase):
    def test_precomputed_path_needs_no_numpy_and_calculation_fails_explicitly(self):
        for failure in (ImportError("numpy unavailable"), RuntimeError("broken binary dependency")):
            with self.subTest(failure=type(failure).__name__), patch("backprop.data_types._optional_module", side_effect=failure):
                self.assertEqual(Histogram(np_histogram=([2], [0, 1])).histogram, [2])
                with self.assertRaisesRegex(Error, "Auto creation of histograms requires numpy"):
                    Histogram([0, 1])


if __name__ == "__main__":
    unittest.main()
