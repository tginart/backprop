"""Positive runtime gates for actual Plotly conversion and Polars histories.

Architecture: import installed providers -> execute hash-pinned W&B method
bodies with the same providers -> compare Backprop values/errors and readback.
Only W&B storage/auth boundaries are supplied; Plotly, matplotlib and Polars
are never replaced with recording objects or injected modules. Missing packages
skip explicitly so the strict optional-runtime runner can reject missing gates.
Main callers: ordinary unittest discovery and the strict runtime verifier.
"""
from __future__ import annotations

import copy
import importlib
import importlib.util
import os
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public.runs import Runs
from backprop.errors import CommError
from test_public_api_errors import supplied_requests
from test_public_api_histories import SOURCE, reference_histories, run_fixtures
from test_sdk_plotly_contract import HAS_REFERENCE, load_nodes, official_plotly


def available(*names):
    return all(importlib.util.find_spec(name) is not None for name in names)


def reference_typenames():
    """Load the pinned dispatch predicates without importing optional providers."""
    namespace = {}
    load_nodes("wandb/util.py", {"get_full_typename", "is_matplotlib_typename", "is_plotly_figure_typename"}, namespace)
    return namespace


def reference_converter(directory):
    """Add unchanged conversion helpers to the pinned Plotly storage wrapper.

    Main callers: the genuine matplotlib test. The wrapper's storage is local;
    its converter imports the actual installed matplotlib and plotly.tools.
    """
    wrapper = official_plotly(directory)
    namespace = reference_typenames()
    namespace["get_module"] = lambda name, required=None: importlib.import_module(name)
    load_nodes("wandb/util.py", {
        "ensure_matplotlib_figure", "matplotlib_to_plotly", "matplotlib_contains_images",
    }, namespace)
    utility = wrapper.__init__.__globals__["util"]
    for name in ("get_full_typename", "is_matplotlib_typename", "is_plotly_figure_typename",
                 "ensure_matplotlib_figure", "matplotlib_to_plotly", "matplotlib_contains_images"):
        setattr(utility, name, namespace[name])
    return wrapper


@unittest.skipUnless(HAS_REFERENCE, "Pinned W&B source is required")
class ProviderReferenceFixtureContracts(unittest.TestCase):
    def test_pinned_typename_preserves_module_dispatch_without_optional_packages(self):
        utility = reference_typenames()
        module = ModuleType("matplotlib.pyplot")
        self.assertEqual(utility["get_full_typename"](module), "matplotlib.pyplot")
        self.assertTrue(utility["is_matplotlib_typename"](utility["get_full_typename"](module)))
        self.assertFalse(utility["is_plotly_figure_typename"](utility["get_full_typename"](module)))
        self.assertEqual(utility["get_full_typename"](object()), "builtins.object")
        self.assertTrue(utility["is_plotly_figure_typename"]("plotly.graph_objs._figure.Figure"))


@unittest.skipUnless(HAS_REFERENCE and available("numpy", "matplotlib", "plotly"),
                     "Genuine NumPy/matplotlib/Plotly and pinned source are required")
class GenuineMatplotlibPlotlyContracts(unittest.TestCase):
    def test_real_converter_matches_pinned_and_survives_run_log_readback(self):
        with tempfile.TemporaryDirectory(prefix="backprop-genuine-converter-") as directory:
            with patch.dict(os.environ, {"MPLBACKEND": "Agg", "MPLCONFIGDIR": directory}):
                import matplotlib
                matplotlib.use("Agg", force=True)
                import matplotlib.pyplot as pyplot
                import numpy as np
                import plotly.tools

                # A non-raster artist must use the genuine mpl_to_plotly path.
                x = np.array([0., 1., 2., 3.])
                y = np.array([3., 1., 4., 2.])
                figures = []

                def new_input(name):
                    """Create an equivalent active figure for each converter call.

                    Main callers: reference comparisons and run-log setup below.
                    Plotly's exporter closes its input figure, so pyplot must
                    start with a fresh current figure for each implementation.
                    """
                    figure, axis = pyplot.subplots(figsize=(4, 3), dpi=100)
                    figures.append(figure)
                    axis.plot(x, y, marker="o", color="#4c9be8", label="loss")
                    axis.set(xlabel="epoch", ylabel="loss", title="Genuine converter")
                    return {"figure": figure, "axes": axis, "pyplot": pyplot}[name]

                try:
                    reference = reference_converter(directory)
                    expected = reference.make_plot_media(new_input("figure")).spec
                    self.assertTrue(expected["data"])
                    self.assertEqual(list(expected["data"][0]["x"]), x.tolist())
                    self.assertEqual(list(expected["data"][0]["y"]), y.tolist())
                    for name in ("figure", "axes", "pyplot"):
                        with self.subTest(input=name):
                            actual = backprop.Plotly.make_plot_media(new_input(name))
                            self.assertIsInstance(actual, backprop.Plotly)
                            self.assertEqual(actual.spec, reference.make_plot_media(new_input(name)).spec)

                    sink = backprop.MemorySink()
                    with backprop.Run(project="genuine-converter", entity="tests", id="line",
                            dir=directory, settings={"console": "off", "silent": True,
                                "disable_git": True, "label_disable": True},
                            _sink=sink, _deployment={}) as run:
                        run.log({"pending": 7}, commit=False)
                        figure = new_input("figure")
                        axis = figure.axes[0]
                        # Convert the current pyplot figure before the exporter
                        # closes it. Explicit Figure/Axes references remain valid.
                        run.log({"pyplot": pyplot, "figure": figure, "axes": axis})
                        run.summary["converted"] = backprop.Plotly.make_plot_media(figure)
                    remote = backprop.Api._from_store(sink, entity="tests", project="genuine-converter").run("line")
                    rows = list(remote.scan_history())
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["pending"], 7)
                    for name in ("figure", "axes", "pyplot"):
                        self.assertEqual(rows[0][name]["_type"], "plotly")
                        self.assertEqual(rows[0][name]["spec"], expected)
                    self.assertEqual(remote.summary["converted"]["spec"], expected)
                    self.assertEqual(remote.state, "finished")
                finally:
                    for figure in figures:
                        pyplot.close(figure)


@unittest.skipUnless(SOURCE.is_file() and available("polars"),
                     "Genuine Polars and pinned source are required")
class GenuinePolarsHistoryContracts(unittest.TestCase):
    def test_real_per_run_frames_schema_errors_and_empty_results_match_pinned(self):
        import polars as pl
        from polars.testing import assert_frame_equal

        cases = [
            ("matching", [("one", [{"loss": 3, "epoch": 0}]), ("empty", []),
                          ("two", [{"loss": 2, "epoch": 1}, {"loss": 1, "epoch": 2}])]),
            ("empty_collection", []),
            ("empty_runs", [("one", []), ("two", [])]),
            ("replace_run_id", [("one", [{"run_id": "old", "x": 1}])]),
            ("different_columns", [("one", [{"x": 1}]), ("two", [{"other": 2, "extra": 3}])]),
            ("integer_then_boolean", [("one", [{"x": 1}]), ("two", [{"x": True}])]),
            ("integer_then_null", [("one", [{"x": 1}]), ("two", [{"x": None}])]),
        ]
        errors, frames = [], []
        for name, batches in cases:
            with self.subTest(case=name):
                before = copy.deepcopy(batches)
                outcomes, histories = [], []
                for implementation in (reference_histories(), Runs.histories):
                    calls = []
                    # HTTP/service identities support the unchanged public
                    # decorator; the imported Polars module remains genuine.
                    with supplied_requests():
                        try:
                            result = implementation(run_fixtures(calls, batches), samples=7,
                                keys=None, x_axis="epoch", format="polars", stream="system")
                        except Exception as error:
                            self.assertIsInstance(error, CommError)
                            self.assertTrue(type(error.exc).__module__.startswith("polars."))
                            outcomes.append(("error", type(error), str(error), type(error.exc), str(error.exc)))
                        else:
                            self.assertIsInstance(result, pl.DataFrame)
                            outcomes.append(("frame", result))
                    histories.append(calls)
                self.assertEqual(histories[0], histories[1])
                self.assertEqual(batches, before)
                if outcomes[0][0] == "error":
                    errors.append(name)
                    self.assertEqual(outcomes[0], outcomes[1])
                else:
                    frames.append(name)
                    self.assertEqual(outcomes[0][0], "frame")
                    self.assertEqual(outcomes[1][0], "frame")
                    expected, actual = outcomes[0][1], outcomes[1][1]
                    assert_frame_equal(actual, expected)
                    self.assertEqual(actual.schema, expected.schema)
                    self.assertEqual(actual.columns, sorted(actual.columns))
                    if actual.height:
                        self.assertEqual(actual["run_id"].to_list(), [identity for identity, rows in batches for _ in rows])
                    else:
                        self.assertEqual(actual.shape, (0, 0))
        # Let the actual pinned/provider combination decide null/bool inference.
        # Still require genuine successful frames and a real schema failure so
        # this gate cannot pass having exercised just one side of the contract.
        self.assertIn("matching", frames)
        self.assertTrue(errors, "The provider matrix did not exercise any real schema error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
