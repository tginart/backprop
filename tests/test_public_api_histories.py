"""Compare combined history dataframe behavior with pinned Runs.histories.

Architecture: hash-checked official method -> identical run fixtures -> genuine
pandas dataframes and a recording Polars interface -> compare results/call order.
The Polars fixture does not implement inference or prove its runtime semantics;
it proves per-run construction, vertical concatenation and exception boundaries.
Main callers: dependency-free and scientific release test discovery. No installs.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
from backprop.apis.public.runs import Runs
from backprop.errors import Error, CommError
from test_public_api_errors import reference_normalize, supplied_requests

SOURCE = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/apis/public/runs.py"
SOURCE_SHA = "2d2db69df0175276a3b620094364a848ee2c2857ce123a8656b11e14bddba8da"


def reference_histories():
    """Execute the unchanged method body with only its import boundary supplied."""
    data = SOURCE.read_bytes()
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA:
        raise AssertionError("Unexpected pinned W&B0.30.0 Runs source")
    cls = next(node for node in ast.parse(data).body if isinstance(node, ast.ClassDef) and node.name == "Runs")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "histories")
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method], type_ignores=[]))
    def get_module(name, required=None):
        try:
            return __import__(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
            raise Error(required) from None
    namespace = {"util": SimpleNamespace(get_module=get_module), "normalize_exceptions": reference_normalize()}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["histories"]


class ProviderError(Exception):
    pass


class RecordingPolars:
    """Record calls; injected errors stand for failures from the real provider."""
    def __init__(self, calls, fail=None):
        self.calls, self.fail = calls, fail

    def record(self, name, *values):
        self.calls.append((name, copy.deepcopy(values)))
        if name == self.fail:
            raise ProviderError(name)

    def from_records(self, rows):
        self.record("from_records", rows)
        return Frame(self, rows)

    def from_dicts(self, rows):
        raise AssertionError("The pinned branch constructs each run with from_records")

    def lit(self, value):
        self.record("lit", value)
        provider = self
        class Literal:
            def alias(self, name):
                provider.record("alias", name)
                return name, value
        return Literal()

    def DataFrame(self):
        self.record("empty")
        return Frame(self, [])

    def concat(self, frames, *, how):
        self.record("concat", [frame.rows for frame in frames], how)
        return Frame(self, [row for frame in frames for row in frame.rows])


class Frame:
    def __init__(self, provider, rows):
        self.provider, self.rows = provider, copy.deepcopy(rows)
        self.columns = list(dict.fromkeys(key for row in rows for key in row))

    def with_columns(self, expression):
        self.provider.record("with_columns", expression)
        name, value = expression
        return Frame(self.provider, [{**row, name: value} for row in self.rows])

    def select(self, columns):
        self.provider.record("select", columns)
        return Frame(self.provider, [{key: row.get(key) for key in columns} for row in self.rows])


def run_fixtures(calls, batches):
    result = []
    for identity, rows in batches:
        def history(*args, _id=identity, _rows=rows, **kwargs):
            calls.append(("history", _id, args, copy.deepcopy(kwargs)))
            return copy.deepcopy(_rows)
        result.append(SimpleNamespace(id=identity, history=history))
    return result


@unittest.skipUnless(SOURCE.is_file(), "Pinned W&B0.30.0 source snapshot required")
class PolarsHistoryContracts(unittest.TestCase):
    def compare(self, batches, *, fail=None):
        results = []
        for implementation in (reference_histories(), Runs.histories):
            calls = []
            provider = RecordingPolars(calls, fail)
            runs = run_fixtures(calls, batches)
            with supplied_requests(), patch.dict(sys.modules, {"polars": provider}):
                try:
                    result = implementation(runs, samples=7, keys=["loss"], x_axis="epoch", format="polars", stream="system")
                except Exception as error:
                    result = (type(error), str(error))
                else:
                    result = result.rows
            results.append((calls, result))
        self.assertEqual(results[0], results[1])
        return results[1]

    def test_per_run_frames_vertical_concat_sorted_columns_and_forwarded_arguments(self):
        batches = [("one", [{"loss": 3, "epoch": 0}]), ("empty", []),
                   ("two", [{"loss": 2, "epoch": 1}, {"loss": 1, "epoch": 2}])]
        before = copy.deepcopy(batches)
        calls, rows = self.compare(batches)
        self.assertEqual(batches, before)
        self.assertEqual([row["run_id"] for row in rows], ["one", "two", "two"])
        self.assertEqual(len([call for call in calls if call[0] == "from_records"]), 2)
        self.assertEqual(next(call for call in calls if call[0] == "concat")[1][1], "vertical")
        self.assertEqual(list(rows[0]), ["epoch", "loss", "run_id"])

    def test_empty_collection_and_empty_runs_return_empty_frame_without_concat(self):
        for batches in ([], [("empty", [])], [("one", []), ("two", [])]):
            with self.subTest(batches=batches):
                calls, result = self.compare(batches)
                self.assertEqual(result, [])
                self.assertEqual(calls[-1], ("empty", ()))
                self.assertFalse(any(call[0] == "concat" for call in calls))

    def test_provider_errors_propagate_at_same_per_run_or_concat_boundary(self):
        batches = [("one", [{"loss": 3}]), ("two", [{"different": "value"}])]
        for operation in ("from_records", "with_columns", "concat", "select"):
            with self.subTest(operation=operation):
                calls, result = self.compare(batches, fail=operation)
                self.assertEqual(result, (CommError, operation))
                self.assertEqual(calls[-1][0], operation)
        # These are provider error sentinels, not a simulation or assertion of
        # the precise schema error that a genuine Polars version would raise.

    def test_missing_dependency_fails_before_history_reads(self):
        calls = []
        with patch.dict(sys.modules, {"polars": None}):
            with self.assertRaisesRegex(Error, "^Exporting polars DataFrame requires polars$"):
                Runs.histories(run_fixtures(calls, [("one", [{"loss": 1}])]), format="polars")
        self.assertEqual(calls, [])


@unittest.skipUnless(SOURCE.is_file(), "Pinned W&B0.30.0 source snapshot required")
class HistoryCombinationContracts(unittest.TestCase):
    def test_default_preserves_pinned_returned_row_identity_and_empty_runs(self):
        for implementation in (reference_histories(), Runs.histories):
            rows = [{"loss": 2}, {"loss": 1, "run_id": "old"}]
            calls = []
            def history(**kwargs):
                calls.append(kwargs)
                return rows
            run = SimpleNamespace(id="one", history=history)
            with supplied_requests():
                result = implementation([run, SimpleNamespace(id="empty", history=lambda **kwargs: [])], samples=4, keys=["loss"], x_axis="epoch", stream="system")
            self.assertIs(result[0], rows[0])
            self.assertIs(result[1], rows[1])
            self.assertEqual(rows, [{"loss": 2, "run_id": "one"}, {"loss": 1, "run_id": "one"}])
            self.assertEqual(calls, [{"samples": 4, "keys": ["loss"], "x_axis": "epoch", "pandas": False, "stream": "system"}])

    def test_signature_and_invalid_format_match_pinned_method_body(self):
        expected = [(key, value.default) for key, value in inspect.signature(reference_histories()).parameters.items()]
        actual = [(key, value.default) for key, value in inspect.signature(Runs.histories).parameters.items()]
        self.assertEqual(actual, expected)
        for value in (None, "csv", [], {}):
            outcomes = []
            for implementation in (reference_histories(), Runs.histories):
                try:
                    with supplied_requests():
                        implementation([], format=value)
                except Exception as error:
                    outcomes.append((type(error), str(error)))
            self.assertEqual(len(outcomes), 2)
            self.assertEqual(outcomes[0], outcomes[1])

    def test_missing_dataframe_provider_is_reported_before_history_reads(self):
        for name in ("pandas", "polars"):
            calls = []
            for implementation in (reference_histories(), Runs.histories):
                with supplied_requests(), patch.dict(sys.modules, {name: None}):
                    with self.assertRaisesRegex(Error, f"^Exporting {name} DataFrame requires {name}$"):
                        implementation(run_fixtures(calls, [("one", [{"loss": 1}])]), format=name)
            self.assertFalse(calls)


@unittest.skipUnless(SOURCE.is_file() and importlib.util.find_spec("pandas"), "Pinned source and genuine pandas are required")
class PandasHistoryContracts(unittest.TestCase):
    def compare(self, batches):
        import pandas as pd
        before = copy.deepcopy(batches)
        expected_calls, actual_calls = [], []
        with supplied_requests():
            expected = reference_histories()(run_fixtures(expected_calls, batches), samples=5, keys=None, x_axis="epoch", format="pandas", stream="system")
        actual = Runs.histories(run_fixtures(actual_calls, batches), samples=5, keys=None, x_axis="epoch", format="pandas", stream="system")
        pd.testing.assert_frame_equal(actual, expected)
        self.assertEqual(actual_calls, expected_calls)
        self.assertEqual(batches, before)
        return actual

    def test_genuine_per_run_inference_preserves_pinned_none_bool_and_sparse_dtypes(self):
        cases = [[("one", [{"x": 1}]), ("two", [{"x": None}])],
                 [("one", [{"x": 1}]), ("two", [{"x": True}])],
                 [("one", [{"x": 1}]), ("two", [{"different": "text"}])],
                 [("one", [{"x": 1}, {"x": None}]), ("two", [{"x": 2.5}])]]
        for batches in cases:
            with self.subTest(batches=batches):
                result = self.compare(batches)
                self.assertEqual(list(result.index), list(range(sum(len(rows) for _, rows in batches))))
                self.assertEqual(list(result.columns), sorted(result.columns))

    def test_genuine_empty_runs_and_run_id_override_match_pinned(self):
        for batches in ([], [("empty", [])], [("empty", []), ("one", [{"run_id": "old", "x": 1}])]):
            with self.subTest(batches=batches):
                result = self.compare(batches)
                if len(result):
                    self.assertEqual(list(result["run_id"]), ["one"])
                else:
                    self.assertEqual(list(result.columns), [])


if __name__ == "__main__":
    unittest.main()
