"""Exercise genuine matplotlib inputs without writing user renderer state.

Architecture: Agg draws a fixed four-color image -> Backprop dispatch encodes it
-> Pillow verifies actual PNG dimensions and representative pixels. Figure,
Axes, and pyplot inputs use pinned W&B's shared figure-selection contract.
Main callers: local discovery (explicit optional skip) or the existing remote
crowdsep runtime. MPLCONFIGDIR lives in a temporary directory; no GUI is opened.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
from backprop import Image, Plotly, MemorySink, Run
from backprop.apis.public.replay import reduce_events

AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("matplotlib", "numpy", "PIL"))


class PlotlyInputBoundary(unittest.TestCase):
    def test_invalid_values_fail_before_loading_optional_plotly(self):
        with patch("backprop.media._optional_module", side_effect=AssertionError("unexpected optional import")):
            for value in (None, 1, 0.5, "invalid", object()):
                with self.subTest(type=type(value).__name__), self.assertRaises(ValueError):
                    Plotly(value)
            spec = {"data": [{"type": "scatter", "x": [0], "y": [1]}]}
            self.assertEqual(Plotly(spec).spec, spec)

    def test_run_log_recognizes_plotly_figure_fixture_and_rejects_other_json_objects(self):
        spec = {"data": [{"type": "scatter", "x": [0, 1], "y": [1, 3]}], "layout": {"title": {"text": "fixture"}}}
        def forbidden_figure_json(self):
            raise AssertionError("Figure.to_json is not the pinned conversion")
        figure_type = type("Figure", (), {"__module__": "plotly.graph_objs._figure",
                                         "to_plotly_json": lambda self: spec,
                                         "to_json": forbidden_figure_json})
        forbidden_calls = []
        def forbidden_json(self):
            forbidden_calls.append(type(self).__name__)
            return "{}"
        layout_type = type("Layout", (), {"__module__": "plotly.graph_objs._layout", "to_json": forbidden_json})
        unrelated_type = type("Custom", (), {"to_json": forbidden_json})
        for value in (layout_type(), unrelated_type()):
            with self.assertRaisesRegex(ValueError, "Logged plots must"):
                Plotly(value)
        with tempfile.TemporaryDirectory() as root:
            sink = MemorySink()
            with Run(project="plotly-fixture", dir=root, settings={"console": "off", "silent": True, "disable_git": True}, _sink=sink, _deployment={}) as run:
                run.log({"pending": 7}, commit=False)
                for invalid, error_type in [(layout_type(), ValueError), (unrelated_type(), TypeError)]:
                    count = run._journal.count
                    with self.subTest(type=type(invalid).__name__), self.assertRaises(error_type):
                        run.log({"invalid": invalid})
                    self.assertEqual(run._journal.count, count)
                    self.assertEqual(run.step, 0)
                run.log({"figure": figure_type(), "wrapped": Plotly(spec)})
            history = reduce_events(sink.events)["history"]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["pending"], 7)
            self.assertEqual(history[0]["figure"], {"_type": "plotly", "spec": spec})
            self.assertEqual(history[0]["wrapped"], history[0]["figure"])
            self.assertEqual(forbidden_calls, [])


@unittest.skipUnless(AVAILABLE, "Genuine matplotlib/NumPy/Pillow runtime is not installed")
class MatplotlibRuntimeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="backprop-matplotlib-",
                                                   dir=os.environ.get("BACKPROP_TEST_TEMP"))
        cls.environment = patch.dict(os.environ, {"MPLBACKEND": "Agg", "MPLCONFIGDIR": cls.temporary.name})
        cls.environment.start()
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as pyplot
        import numpy
        from PIL import Image as PillowImage
        cls.pyplot, cls.numpy, cls.PillowImage = pyplot, numpy, PillowImage

    @classmethod
    def tearDownClass(cls):
        cls.pyplot.close("all")
        cls.environment.stop()
        cls.temporary.cleanup()

    def setUp(self):
        self.figure = self.pyplot.figure(figsize=(2, 1), dpi=100)
        self.axis = self.figure.add_axes([0, 0, 1, 1])
        pixels = self.numpy.array([[[255, 0, 0], [0, 255, 0]],
                                  [[0, 0, 255], [255, 255, 255]]], dtype="uint8")
        self.axis.imshow(pixels, interpolation="nearest", aspect="auto")
        self.axis.set_axis_off()

    def tearDown(self):
        self.pyplot.close(self.figure)

    def assert_png(self, value):
        self.assertIsInstance(value, Image)
        self.assertEqual(value.metadata["mime_type"], "image/png")
        self.assertEqual((value.metadata["width"], value.metadata["height"]), (200, 100))
        with self.PillowImage.open(io.BytesIO(value._bytes())) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (200, 100))
            rgb = image.convert("RGB")
            for position, expected in [((25, 25), (255, 0, 0)), ((175, 25), (0, 255, 0)),
                                       ((25, 75), (0, 0, 255)), ((175, 75), (255, 255, 255))]:
                self.assertEqual(rgb.getpixel(position), expected)

    def test_image_accepts_figure_axes_and_pyplot_with_exact_pixels(self):
        numbers = self.pyplot.get_fignums()
        for name, value in [("figure", self.figure), ("axes", self.axis), ("pyplot", self.pyplot)]:
            with self.subTest(input=name):
                encoded = Image(value, caption="real matplotlib")
                self.assert_png(encoded)
                self.assertEqual(encoded.caption, "real matplotlib")
                self.assertIs(self.pyplot.gcf(), self.figure)
                self.assertEqual(self.pyplot.get_fignums(), numbers)

    def test_make_plot_media_routes_image_figures_without_plotly_dependency(self):
        for name, value in [("figure", self.figure), ("axes", self.axis), ("pyplot", self.pyplot)]:
            with self.subTest(input=name):
                self.assert_png(Plotly.make_plot_media(value))

    def test_direct_plotly_rejects_raster_figure_before_optional_conversion(self):
        for name, value in [("figure", self.figure), ("axes", self.axis), ("pyplot", self.pyplot)]:
            with self.subTest(input=name):
                with self.assertRaisesRegex(ValueError, "Image"):
                    Plotly(value)

    def test_interactive_conversion_reports_missing_plotly_explicitly(self):
        if importlib.util.find_spec("plotly") is not None:
            self.skipTest("This dependency-boundary fixture requires matplotlib without Plotly")
        self.axis.images[0].remove()
        self.axis.plot([0, 1], [1, 0])
        with self.assertRaisesRegex(ImportError, "plotly"):
            Plotly.make_plot_media(self.figure)

    def test_bare_run_log_raster_figures_preserve_png_bytes_and_metadata(self):
        expected_bytes = Image(self.figure)._bytes()
        with tempfile.TemporaryDirectory(dir=self.temporary.name) as root:
            sink = MemorySink()
            with Run(project="matplotlib-roundtrip", dir=root, settings={"console": "off", "silent": True, "disable_git": True}, _sink=sink, _deployment={}) as run:
                run.log({"figure": self.figure, "axes": self.axis, "pyplot": self.pyplot})
            history = reduce_events(sink.events)["history"]
            self.assertEqual(len(history), 1)
            for name in ("figure", "axes", "pyplot"):
                with self.subTest(input=name):
                    stored = history[0][name]
                    self.assertEqual(stored["_type"], "image")
                    self.assertEqual((stored["width"], stored["height"], stored["mime_type"]), (200, 100, "image/png"))
                    actual_bytes = sink.download_file(stored)
                    self.assertEqual(actual_bytes, expected_bytes)
                    self.assert_png(Image(actual_bytes))

    def test_bare_run_log_line_figure_requires_plotly_without_committing_bad_row(self):
        if importlib.util.find_spec("plotly") is not None:
            self.skipTest("This dependency-boundary fixture requires matplotlib without Plotly")
        self.axis.images[0].remove()
        self.axis.plot([0, 1], [1, 0])
        with tempfile.TemporaryDirectory(dir=self.temporary.name) as root:
            sink = MemorySink()
            with Run(project="matplotlib-dependency", dir=root, settings={"console": "off", "silent": True, "disable_git": True}, _sink=sink, _deployment={}) as run:
                run.log({"pending": 11}, commit=False)
                count = run._journal.count
                for name, value in [("figure", self.figure), ("pyplot", self.pyplot)]:
                    with self.subTest(input=name), self.assertRaisesRegex(ImportError, "plotly"):
                        run.log({"plot": value})
                    self.assertEqual(run._journal.count, count)
                    self.assertEqual(run.step, 0)
                run.log({"next": 13})
            rows = reduce_events(sink.events)["history"]
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["pending"], rows[0]["next"]), (11, 13))
            self.assertNotIn("plot", rows[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
