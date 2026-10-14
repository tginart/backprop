"""Compare exact pinned ROC/PR wrappers with native chart implementations.

Architecture: execute the unmodified pinned Python module bodies with narrow
W&B import/Table/chart/terminal shims -> use real NumPy/pandas/sklearn -> compare
complete chart rows/specs, exception types/messages, warnings and termwarn text.
Main callers: unittest in an existing scientific runtime. The ignored pinned
snapshot must be present; absence is an explicit skip, not a fabricated oracle.
Why this exists: testing sklearn alone misses wrapper-level defaults, duplicate
labels, interpolation, rounding and resampling behavior. This does not execute
the complete official SDK, its Table implementation or its online service.
"""
import copy
import hashlib
import importlib
import importlib.util
import math
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk"))
import backprop
from backprop import plot

REFERENCE = ROOT / ".reference/wandb/source/wandb/plot"
AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("numpy", "pandas", "scipy", "sklearn"))
REFERENCE_AVAILABLE = all((REFERENCE / name).is_file() for name in ("roc_curve.py", "pr_curve.py", "utils.py"))
REFERENCE_HASHES = {
    "roc_curve.py": "d48e5cec801b8ed72b0f91e9673b9d273b6aa88e5b888c30771c8f6cd6d0379d",
    "pr_curve.py": "d601f0c34b519a9e1bfde3b01c7adbb5c93c260a46c2dede8858af91cb7a69db",
    "utils.py": "b59acc923b3813b57abf1984de809dc57e0ad019042366b9040e1c46d8b97970",
}


def normalized(value):
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalized(item) for item in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    return value


class ReferenceTable:
    MAX_ROWS = backprop.Table.MAX_ROWS

    def __init__(self, dataframe):
        self.columns = list(dataframe.columns)
        self.data = dataframe.values.tolist()


def reference_modules():
    """Execute exact source bytes; replace only the surrounding SDK boundaries."""
    for name, digest in REFERENCE_HASHES.items():
        if hashlib.sha256((REFERENCE / name).read_bytes()).hexdigest() != digest:
            raise AssertionError(f"Statistical oracle is not the pinned W&B 0.30.0 source: {name}")
    sdk = ModuleType("wandb")
    sdk.__path__ = []
    sdk.Table, sdk.termwarn, sdk.termerror = ReferenceTable, lambda text: None, lambda text: None
    util = ModuleType("wandb.util")
    util.get_module = lambda name, *args, **kwargs: importlib.import_module(name)
    sdk.util = util
    plots = ModuleType("wandb.plot")
    plots.__path__ = []
    custom = ModuleType("wandb.plot.custom_chart")
    def chart(vega_spec_name, data_table, fields, string_fields, split_table):
        return SimpleNamespace(table=data_table, spec=SimpleNamespace(spec_name=vega_spec_name,
            fields=fields, string_fields=string_fields, split_table=split_table))
    custom.plot_table = chart
    modules = {"wandb": sdk, "wandb.util": util, "wandb.plot": plots, "wandb.plot.custom_chart": custom}
    with patch.dict(sys.modules, modules):
        for name in ("utils", "roc_curve", "pr_curve"):
            module = ModuleType("wandb.plot." + name)
            module.__file__ = str(REFERENCE / (name + ".py"))
            sys.modules[module.__name__] = module
            exec(compile(Path(module.__file__).read_bytes(), module.__file__, "exec"), module.__dict__)
            modules[name] = module
    return sdk, {name: getattr(modules[name], name) for name in ("roc_curve", "pr_curve")}


def outcome(function, kwargs, terminal_owner):
    """Capture observable wrapper results without suppressing error differences."""
    messages = []
    with warnings.catch_warnings(record=True) as caught, patch.object(terminal_owner, "termwarn", side_effect=lambda text: messages.append(["warning", text])), patch.object(terminal_owner, "termerror", side_effect=lambda text: messages.append(["error", text])):
        warnings.simplefilter("always")
        try:
            chart = function(**kwargs)
            if chart is None:
                result = {"return": None}
            else:
                result = {"columns": chart.table.columns, "rows": chart.table.data,
                          "spec": chart.spec.spec_name, "fields": chart.spec.fields,
                          "strings": chart.spec.string_fields, "split": chart.spec.split_table}
        except Exception as error:
            result = {"error": type(error).__name__, "message": str(error)}
    return normalized({"result": result, "warnings": [[type(item.message).__name__, str(item.message)] for item in caught], "terminal": messages})


class DependencyBoundary(unittest.TestCase):
    def test_missing_scientific_dependency_is_explicit_without_installing(self):
        for name in ("roc_curve", "pr_curve"):
            for missing in ("numpy", "pandas", "sklearn.metrics", "sklearn.utils"):
                for failure in (ImportError("not installed"), RuntimeError("broken binary dependency")):
                    def importing(module):
                        if module == missing:
                            raise failure
                        return object()
                    with self.subTest(api=name, missing=missing, failure=type(failure).__name__), patch("backprop.statistical_curves.importlib.import_module", side_effect=importing):
                        with self.assertRaisesRegex(backprop.Error, "optional " + missing):
                            getattr(plot, name)([0, 1], [[0.9, 0.1], [0.1, 0.9]])


@unittest.skipUnless(AVAILABLE and REFERENCE_AVAILABLE, "Pinned W&B sources and optional NumPy/pandas/scipy/sklearn runtime are required")
class StatisticalCurveReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sdk, cls.reference = reference_modules()

    def compare(self, name, kwargs, limit=None):
        with patch.object(backprop.Table, "MAX_ROWS", limit or 10000), patch.object(ReferenceTable, "MAX_ROWS", limit or 10000):
            expected = outcome(self.reference[name], copy.deepcopy(kwargs), self.sdk)
            actual = outcome(getattr(plot, name), copy.deepcopy(kwargs), backprop)
        self.assertEqual(actual, expected, f"{name}: {kwargs}")
        return actual

    def test_defaults_class_labels_scores_and_warning_cases(self):
        cases = {
            "binary": {"y_true": [0, 1, 0, 1], "y_probas": [[0.9, 0.1], [0.2, 0.8], [0.6, 0.4], [0.5, 0.5]]},
            "ties": {"y_true": [0, 1, 0, 1], "y_probas": [[0.5, 0.5]] * 4},
            "one_class_zero": {"y_true": [0, 0, 0], "y_probas": [[0.1], [0.5], [0.8]]},
            "one_sample": {"y_true": [1], "y_probas": [[0.8, 0.2]]},
            "one_class_string": {"y_true": ["yes", "yes"], "y_probas": [[0.2], [0.8]]},
            "string_labels": {"y_true": ["cat", "dog", "dog"], "y_probas": [[0.8, 0.2], [0.1, 0.9], [0.3, 0.7]], "labels": ["ignored"]},
            "nonconsecutive": {"y_true": [2, 4, 2, 4], "y_probas": [[-10, 10], [1, -1], [0, 0], [0.2, 0.8]]},
            "extra_columns": {"y_true": [0, 1], "y_probas": [[0.8, 0.2, 5], [0.1, 0.9, 7]]},
            "duplicate_display_labels": {"y_true": [0, 1, 0], "y_probas": [[0.7, 0.3], [0.1, 0.9], [0.6, 0.4]], "labels": ["same", "same"]},
            "selected_and_unknown": {"y_true": [0, 1], "y_probas": [[0.7, 0.3], [0.2, 0.8]], "classes_to_plot": [1, 99], "title": "Selected", "split_table": True},
            "column_vector": {"y_true": [[0], [1]], "y_probas": [[0.7, 0.3], [0.2, 0.8]]},
            "boolean_labels": {"y_true": [False, True], "y_probas": [[0.7, 0.3], [0.2, 0.8]], "labels": ["false", "true"]},
            "float_integer_labels": {"y_true": [0.0, 1.0], "y_probas": [[0.7, 0.3], [0.2, 0.8]], "labels": ["ignored0", "ignored1"]},
            "negative_labels": {"y_true": [-1, 0], "y_probas": [[0.7, 0.3], [0.2, 0.8]], "labels": ["zero", "negative"]},
            "numeric_string_scores": {"y_true": [0, 1], "y_probas": [["0.7", "0.3"], ["0.2", "0.8"]]},
        }
        for name in self.reference:
            for case, kwargs in cases.items():
                with self.subTest(api=name, case=case):
                    self.compare(name, kwargs)

    def test_degenerate_errors_empty_selection_and_nonfinite_inputs(self):
        base = {"y_true": [0, 1], "y_probas": [[0.7, 0.3], [0.2, 0.8]]}
        cases = [{}, {"y_true": None}, {"y_true": []}, {"y_true": [], "y_probas": []},
                 {"y_true": 1, "y_probas": 0.8}, {**base, "classes_to_plot": []},
                 {**base, "classes_to_plot": [99]}, {**base, "labels": ["short"]},
                 {**base, "labels": [[], []]}, {**base, "y_probas": [0.1, 0.9]},
                 {**base, "y_probas": [[0.1], [0.9]]}, {**base, "y_probas": [[0.1, 0.9]]},
                 {**base, "y_probas": [[0.1], [0.2, 0.8]]}, {**base, "y_true": [0.1, 0.8]},
                 {**base, "y_probas": [[float("nan"), 0.3], [0.2, 0.8]]},
                 {**base, "y_probas": [[float("inf"), 0.3], [0.2, 0.8]]},
                 {**base, "y_true": [0, float("nan")]}, {**base, "y_true": [0, float("inf")]},
                 {**base, "y_probas": [["low", "high"], ["high", "low"]]}]
        for name in self.reference:
            for index, kwargs in enumerate(cases):
                # roc_curve has one required positional argument in both APIs.
                with self.subTest(api=name, case=index):
                    self.compare(name, kwargs)

    def test_precision_recall_interpolation_type_and_size_boundaries(self):
        import numpy as np
        base = {"y_true": [0, 1, 0, 1], "y_probas": [[0.9, 0.1], [0.2, 0.8], [0.6, 0.4], [0.5, 0.5]]}
        for size in (0, 1, 2, 3, 21, np.int64(7), True, False, -1, 2.5, None, "3"):
            with self.subTest(interp_size=repr(size)):
                self.compare("pr_curve", {**base, "interp_size": size})

    def test_arrays_dataframes_resampling_and_rounding_match_exact_wrapper(self):
        import numpy as np
        import pandas as pd
        rng = np.random.default_rng(730)
        for index in range(8):
            truth = np.arange(93) % 3
            scores = rng.integers(0, 100, (93, 3)) / 100
            for name in self.reference:
                for limit in (None, 17):
                    with self.subTest(api=name, trial=index, rows=limit):
                        self.compare(name, {"y_true": pd.Series(truth), "y_probas": pd.DataFrame(scores)}, limit)
        for name in self.reference:
            with self.subTest(api=name, impossible_stratification=True):
                self.compare(name, {"y_true": [0, 1, 2], "y_probas": [[0.8, 0.1, 0.1], [0.1, 0.8, 0.1], [0.1, 0.1, 0.8]]}, limit=2)

    def test_original_input_arrays_are_unchanged(self):
        import numpy as np
        truth = np.array([0, 1, 0, 1])
        scores = np.array([[0.9, 0.1], [0.2, 0.8], [0.6, 0.4], [0.5, 0.5]])
        original_truth, original_scores = truth.copy(), scores.copy()
        for name in self.reference:
            getattr(plot, name)(truth, scores)
            np.testing.assert_array_equal(truth, original_truth)
            np.testing.assert_array_equal(scores, original_scores)


if __name__ == "__main__":
    unittest.main(verbosity=2)
