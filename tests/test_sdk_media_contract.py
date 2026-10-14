"""Behavioral media/table/chart contracts without network or package installation.

Architecture: public values -> Encoder -> captured references. Array/PIL cases
run when those optional packages already exist, while scalar/file contracts are
always checked. Main callers: unittest discovery in either available runtime.
"""
import base64
import importlib.util
import io
import json
import math
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
from backprop.data_types import Table, Histogram, Image, Audio, Video, Html, Classes, Encoder, BoundingBoxes2D, ImageMask
from backprop import plot, Error, Artifact

HAS_ARRAYS = importlib.util.find_spec("numpy") is not None and importlib.util.find_spec("PIL") is not None
HAS_STATISTICS = all(importlib.util.find_spec(name) is not None for name in ("numpy", "pandas", "sklearn"))
HAS_VIDEO = HAS_ARRAYS and all(importlib.util.find_spec(name) is not None for name in ("moviepy", "imageio", "packaging"))
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5i8AAAAASUVORK5CYII=")


def encoder():
    def stage(path, data=None, name=None):
        return {"path": name, "bytes": len(Path(path).read_bytes() if path else data)}
    return Encoder(stage)


class TableContracts(unittest.TestCase):
    def test_table_limit_warning_strict_error_and_incremental_recovery(self):
        table = Table(columns=["x"], data=[[1], [2], [3]], log_mode="INCREMENTAL")
        table.MAX_ARTIFACT_ROWS = 2
        encoder1 = encoder()
        with self.assertWarns(RuntimeWarning):
            result = encoder1.encode(table)
        self.assertEqual(result["data"], [[1], [2]])
        encoder1.commit()
        self.assertEqual(encoder().encode(table)["data"], [[3]])
        table._offset = 0
        strict = Encoder(lambda *args, **kwargs: {}, settings=SimpleNamespace(strict=True))
        with self.assertRaises(ValueError):
            strict.encode(table)
        self.assertEqual(table._offset, 0)

    def test_keys_indices_class_schemas_and_atomic_relationship_promotion(self):
        source = Table(columns=["id", "label"], data=[["a", "first"], ["b", "second"]])
        source.set_pk("id")
        child = Table(columns=["key", "index"], data=[["a", 0]], optional=False)
        child.set_fk("key", source, "id")
        child.add_data(source.get_column("id")[1], source.index_ref(1))
        self.assertEqual(child.data[1][1].get_row(), {"id": "b", "label": "second"})
        self.assertIs(child.data[0][1].get_table(), source)
        self.assertEqual(encoder().encode(child)["column_types"][1]["wb_type"], "foreignIndex")
        self.assertIn(source._id, encoder().encode(child)["related_tables"])
        before = child._types[:]
        with self.assertRaises(TypeError):
            child.add_data(12, source.index_ref(0))
        self.assertEqual(child._types, before)
        with self.assertRaises(TypeError):
            child.cast("key", str)
        with self.assertRaises(AssertionError):
            source.set_pk("label")
        classes = Classes([{"id": 0, "name": "background"}, {"id": 1, "name": "foreground"}])
        table = Table(columns=["class"], data=[[0]], dtype=classes.get_type(), optional=False)
        table.add_data(1)
        with self.assertRaises(TypeError):
            table.add_data(2)

    def test_failed_row_does_not_mutate_inferred_schema(self):
        from backprop.tables import ColumnType
        table = Table(columns=["new", "fixed"], dtype=[ColumnType("unknown", False), int], optional=False)
        with self.assertRaises(TypeError):
            table.add_data("bad-inference", "not-a-number")
        table.add_data(2, 3)
        self.assertEqual(table.data, [[2, 3]])

    def test_failed_column_and_computed_columns_are_atomic(self):
        table = Table(columns=["x"], data=[[1], [2]])
        with self.assertRaises(TypeError):
            table.add_column("bad", [1, "text"])
        self.assertEqual(table.columns, ["x"])
        # Pinned Table permits a local self-index declaration. A native
        # immutable snapshot rejects the resulting dependency cycle explicitly.
        self_table = Table(columns=["x"], data=[[1], [2]])
        self_table.add_column("self", self_table.get_index())
        with self.assertRaisesRegex(ValueError, "cycle"):
            Artifact("self-index", "dataset").add(self_table, "table")
        self.assertEqual(table.columns, ["x"])
        self.assertEqual(table._optional, [True])
        self.assertEqual(table.data, [[1], [2]])
        with self.assertRaises(TypeError):
            table.add_computed_columns(lambda index, row: {"good": row["x"], "bad": 1 if index == 0 else "text"})
        self.assertEqual(table.columns, ["x"])

    def test_empty_table_first_column_and_integer_column_names(self):
        table = Table(columns=[])
        table.add_column("first", [1, 2])
        self.assertEqual(table.data, [[1], [2]])
        numeric = Table(columns=[1], data=[["a"]])
        self.assertEqual(numeric.get_column(1), ["a"])

    def test_bool_is_not_numeric_and_dtype_cast_is_atomic(self):
        table = Table(columns=["x"], data=[[1]])
        with self.assertRaises(TypeError):
            table.add_data(True)
        before = table._types[0]
        with self.assertRaises(TypeError):
            table.cast("x", str)
        self.assertIs(table._types[0], before)
        self.assertEqual(table.cast("x", float).name, "number")

    def test_public_conversion_and_indices(self):
        table = Table(columns=["x"], data=[[1], [2]])
        indices = table.get_index()
        self.assertEqual(indices, [0, 1])
        self.assertIs(indices[0].get_table(), table)
        table.add_computed_columns(lambda index, row: {"squared": row["x"] ** 2})
        self.assertEqual(table.get_column("squared"), [1, 4])
        self.assertEqual(list(table.iterrows())[1][1], [2, 4])

    def test_incremental_cursor_advances_only_after_commit(self):
        table = Table(columns=["x"], data=[[1]], log_mode="INCREMENTAL")
        enc = encoder()
        with self.assertRaises(TypeError):
            enc.encode({"table": table, "bad": object()})
        self.assertEqual(table._offset, 0)
        enc = encoder()
        first = enc.encode(table)
        self.assertEqual(first["data"], [[1]])
        enc.commit()
        table.add_data(2)
        second = encoder().encode(table)
        self.assertEqual(second["data"], [[2]])
        self.assertEqual(second["row_offset"], 1)
        with self.assertRaises(ValueError):
            table.add_column("blocked", [3, 4])

    def test_immutable_snapshot_and_mutable_generation(self):
        table = Table(columns=["x"], data=[[1]])
        enc = encoder(); initial = enc.encode(table); enc.commit()
        table.add_data(2)
        with self.assertWarns(RuntimeWarning):
            self.assertEqual(encoder().encode(table), initial)
        mutable = Table(columns=["x"], data=[[1]], log_mode="MUTABLE")
        enc = encoder(); enc.encode(mutable); enc.commit()
        mutable.add_column("y", [2])
        self.assertEqual(encoder().encode(mutable)["columns"], ["x", "y"])

    @unittest.skipUnless(HAS_ARRAYS, "NumPy/Pillow are optional dependencies")
    def test_numpy_dataframe_and_conversion(self):
        import numpy as np
        table = Table(columns=["x"], data=np.array([[1], [2]]), dtype=np.float32)
        self.assertEqual(table.get_column("x", convert_to="numpy").tolist(), [1, 2])
        if importlib.util.find_spec("pandas") is not None:
            import pandas as pd
            frame = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
            self.assertEqual(Table(data=frame).get_dataframe().to_dict(), frame.to_dict())
        matrix = Table(columns=["array"], data=[[np.ones((2, 3), dtype=np.float32)]])
        with self.assertRaises(TypeError):
            matrix.add_data(np.ones((3, 2)))
        result = Encoder(lambda *args, **kwargs: {}, context="history").encode(matrix)
        self.assertEqual(result["data"][0][0]["_type"], "ndarray")
        self.assertEqual(result["data"][0][0]["dtype"], "float32")
        typed = Table(columns=["dictionary", "list", "union"], dtype=[{"value": int}, [str], [int, str]], optional=False)
        typed.add_data({"value": 1}, ["a", "b"], "allowed")
        typed.add_data({"value": 2}, [], 2)
        with self.assertRaises(TypeError):
            typed.add_data({"value": "wrong"}, ["a"], 2)


class MediaContracts(unittest.TestCase):
    @unittest.skipUnless(HAS_ARRAYS, "NumPy is optional")
    def test_histogram_extreme_ranges_match_numpy_and_missing_dependency_is_explicit(self):
        import numpy as np
        import warnings
        for values, bins in [([-1e308, 1e308], 4), ([1e308, 1e308], 4), ([1e-320, 2e-320], 64), ([-1e300, 1e300], 64)]:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                try:
                    counts, edges = np.histogram(values, bins=bins)
                except Exception as error:
                    with self.assertRaises(type(error)):
                        Histogram(values, num_bins=bins)
                else:
                    histogram = Histogram(values, num_bins=bins)
                    self.assertEqual(histogram.histogram, counts.tolist())
                    self.assertEqual(histogram.bins, edges.tolist())
        with patch("backprop.data_types._optional_module", side_effect=ImportError("numpy is required")):
            for values in [[-1e308, 1e308], [1e308, 1e308], [1e-320, 2e-320]]:
                with self.assertRaises(Error):
                    Histogram(values)

    def test_public_value_registry_suffixes_and_conversions(self):
        from backprop.values import WBValue
        self.assertIs(WBValue.type_mapping()["table"], Table)
        self.assertIs(WBValue.type_mapping()["image-file"], Image)
        for cls, suffix in [(Table, "table"), (Histogram, "histogram"), (Image, "image-file"), (Audio, "audio-file"), (Video, "video-file"), (Html, "html-file"), (Classes, "classes")]:
            self.assertEqual(cls.with_suffix("item"), "item." + suffix + ".json")
            self.assertEqual(cls.with_suffix("item", "custom"), "item." + suffix + ".custom")
        self.assertIsNone(WBValue.init_from_json({"_type": "unknown"}, None))
        # Native histogram decoding is an explicit extension over the pinned
        # abstract decoder. Keep the useful registry round trip concrete.
        hist = WBValue.init_from_json(Histogram(np_histogram=([1, 1], [1, 1.5, 2])).to_json(), None)
        self.assertEqual(hist.histogram, [1, 1])
        self.assertEqual(Audio.captions([Audio([0.1], 8000), Audio([0.1], 8000, "sample")]), ["", "sample"])
        self.assertFalse(Audio.captions([Audio([0.1], 8000)]))
        self.assertEqual(Audio.sample_rates([Audio([0.1], 8000)]), [8000])
        self.assertEqual(Audio.durations([Audio([0.1], 8000)]), [1/8000])
        for value in [Table(columns=[]), Histogram(np_histogram=([1], [0, 1])), Audio([0.1], 8000), Video(io.BytesIO(b"video"), format="webm"), Html("hello"), Classes([])]:
            with self.assertRaises(NotImplementedError):
                value.to_data_array()

    def test_binding_and_media_collection_helpers_preserve_metadata(self):
        from backprop import Run, MemorySink
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        run = Run(dir=directory.name, _sink=MemorySink(), settings={"console":"off", "silent":True, "x_disable_stats":True})
        self.addCleanup(run.finish)
        images = [Image(PNG, caption="one"), Image(PNG)]
        for value in images:
            self.assertTrue(value.file_is_set())
            self.assertFalse(value.is_bound())
            value.bind_to_run(run, "images", 0)
            self.assertTrue(value.is_bound())
        # Public separated-image serialization reads dimensions through Pillow,
        # as pinned W&B does. Native encoded-byte binding needs no decoder.
        if importlib.util.find_spec("PIL") is None:
            with self.assertRaises(ImportError):
                Image.seq_to_json(images, run, "images", 0)
            return
        collection = Image.seq_to_json(images, run, "images", 0)
        self.assertEqual(collection["captions"], ["one", None])
        self.assertEqual(collection["count"], 2)
        self.assertFalse(Image.all_masks(images, run, "images", 0))
        self.assertFalse(Image.all_boxes(images, run, "images", 0))

    @unittest.skipUnless(HAS_ARRAYS, "NumPy/Pillow are optional")
    def test_reusable_annotations_default_labels_and_image_equality(self):
        import numpy as np
        box = BoundingBoxes2D({"box_data": [{"position": {"middle": np.array([0.5, 0.5]), "width": 1, "height": 1}, "class_id": 3}]}, "predictions")
        mask = ImageMask({"mask_data": np.array([[0, 1], [1, 0]], dtype=np.uint8)}, "labels")
        image = Image(np.zeros((2, 2, 3), dtype=np.uint8), boxes={"prediction": box}, masks={"segmentation": mask})
        encoded = encoder().encode(image)
        self.assertEqual(encoded["boxes"]["prediction"]["class_labels"], {"3": "class_3"})
        self.assertEqual(encoded["masks"]["segmentation"]["class_labels"], {"0": "class_0", "1": "class_1"})
        self.assertEqual(Image(image), image)
        self.assertNotIn("masks", Image(image).metadata)

    @unittest.skipUnless(importlib.util.find_spec("soundfile") is not None and HAS_ARRAYS, "soundfile/NumPy are optional")
    def test_pcm_encoding_matches_real_soundfile_for_float_and_integer_arrays(self):
        import numpy as np
        import soundfile as sf
        for dtype in [np.float32, np.float64, np.int16, np.int32]:
            values = np.array([-0.91, -0.1, 0, 0.1, 0.9], dtype=dtype) if dtype in {np.float32, np.float64} else np.array([-13000, 0, 1999, 32767], dtype=dtype)
            expected = io.BytesIO()
            sf.write(expected, values, 8000, format="wav")
            with wave.open(io.BytesIO(expected.getvalue()), "rb") as source:
                expected_pcm = source.readframes(100)
            with wave.open(io.BytesIO(Audio(values, sample_rate=8000).data), "rb") as source:
                self.assertEqual(source.readframes(100), expected_pcm)

    def test_html_paths_injection_and_class_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "page.html"
            path.write_text("<html><b>Hello</b></html>")
            self.assertIn("<head>", Html(path).html)
            self.assertEqual(Html(path, inject=False).html, path.read_text())
            self.assertEqual(Html(str(path), inject=False, data_is_not_path=True).html, str(path))
        classes = Classes([{"id": 7, "name": "thing"}])
        image = Image(PNG, classes=classes)
        self.assertEqual(encoder().encode(image)["classes"], classes.to_json(None))

    @unittest.skipUnless(importlib.util.find_spec("torch") is not None and HAS_ARRAYS, "torch is optional")
    def test_real_tensor_image_detaches_and_transposes(self):
        import numpy as np
        import torch
        tensor = torch.tensor([[[10., 20.], [30., 40.]], [[50., 60.], [70., 80.]], [[90., 100.], [110., 120.]]], requires_grad=True)
        image = Image(tensor)
        self.assertEqual(np.array(image.to_data_array()).tolist(), tensor.detach().permute(1, 2, 0).numpy().astype(np.uint8).tolist())
        value = torch.arange(64.0, requires_grad=True)
        self.assertEqual(Encoder(lambda *args, **kwargs: {}, context="history").encode(value)["_type"], "histogram")

    @unittest.skipUnless(HAS_ARRAYS, "NumPy/Pillow are optional")
    def test_singleton_images_integer_audio_and_history_arrays(self):
        import numpy as np
        for shape in [(1, 1), (1, 4, 3), (4, 1, 1)]:
            image = Image(np.zeros(shape, dtype=np.uint8))
            self.assertEqual((image.metadata["height"], image.metadata["width"]), shape[:2])
        audio = Audio(np.array([-32768, 0, 16384, 32767], dtype=np.int16), sample_rate=8000)
        with wave.open(io.BytesIO(audio.data), "rb") as source:
            self.assertEqual(source.readframes(4), np.array([-32768, 0, 16384, 32767], dtype="<i2").tobytes())
        values = np.arange(64)
        self.assertEqual(Encoder(lambda *args, **kwargs: {}, context="history").encode(values)["_type"], "histogram")
        self.assertEqual(encoder().encode(values), list(range(64)))

    def test_nonfinite_values_survive_strict_json(self):
        result = encoder().encode([math.nan, math.inf, -math.inf])
        self.assertEqual([value["value"] for value in result], ["NaN", "Infinity", "-Infinity"])
        json.dumps(result, allow_nan=False)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "Automatic histograms require optional NumPy")
    def test_histogram_flattening_and_edges(self):
        self.assertEqual(Histogram([[0, 0.5], [1, 1]], num_bins=2).histogram, [1, 3])
        self.assertEqual(Histogram([], num_bins=2).bins, [0, 0.5, 1])

    def test_histogram_precomputed_contract(self):
        self.assertEqual(Histogram(np_histogram=([-1], [1, 0])).histogram, [-1])
        with self.assertRaises(ValueError):
            Histogram(np_histogram=([1, 2], [0, 1]))

    def test_png_metadata_copy_boxes_and_html(self):
        picture = Image(PNG, caption="sample", boxes={"predictions": {"box_data": [{"position": {"minX": 0, "minY": 0, "maxX": 1, "maxY": 1}, "class_id": 1}], "class_labels": {1: "object"}}})
        encoded = encoder().encode(picture)
        self.assertEqual((encoded["width"], encoded["height"]), (1, 1))
        self.assertEqual(encoded["boxes"]["predictions"]["class_labels"], {"1": "object"})
        self.assertEqual(encoded["mime_type"], "image/png")
        self.assertNotIn("boxes", Image(picture).metadata)
        html = Html(io.StringIO("<h1>Hello</h1>"), inject=False)
        self.assertEqual(html.html, "<h1>Hello</h1>")
        self.assertEqual(encoder().encode(html)["mime_type"], "text/html")

    def test_audio_pcm_duration_channels_and_caption(self):
        audio = Audio([[0, 1], [-1, 0.5]], sample_rate=8000, caption="stereo")
        with wave.open(io.BytesIO(audio.data), "rb") as reader:
            self.assertEqual((reader.getnchannels(), reader.getframerate(), reader.getnframes()), (2, 8000, 2))
        self.assertEqual(audio.metadata["duration"], 2 / 8000)
        self.assertEqual(encoder().encode(audio)["mime_type"], "audio/wav")
        with self.assertRaises(ValueError):
            Audio([0.1, 0.2])

    def test_encoded_video_requires_format_and_preserves_type(self):
        with self.assertRaises(ValueError):
            Video(io.BytesIO(b"encoded"))
        video = Video(io.BytesIO(b"encoded"), format="webm", caption="clip")
        self.assertEqual(encoder().encode(video)["mime_type"], "video/webm")

    @unittest.skipUnless(HAS_ARRAYS, "NumPy/Pillow are optional dependencies")
    def test_real_array_image_mask_encoding(self):
        import numpy as np
        from PIL import Image as PILImage
        raw = np.array([[[255, 0, 0], [0, 255, 0]], [[0, 0, 255], [255, 255, 255]]], dtype=np.uint8)
        image = Image(raw, masks={"labels": {"mask_data": np.array([[0, 1], [1, 0]]), "class_labels": {0: "bg", 1: "fg"}}})
        self.assertEqual(np.array(image.to_data_array()).tolist(), raw.tolist())
        self.assertEqual(encoder().encode(image)["masks"]["labels"]["mask"]["mime_type"], "image/png")

    @unittest.skipUnless(HAS_VIDEO, "NumPy/Pillow/MoviePy/imageio/packaging are optional video dependencies")
    def test_real_batched_gif_encoding(self):
        import numpy as np
        from PIL import Image as PILImage
        frames = np.zeros((3, 2, 3, 2, 2), dtype=np.uint8)
        frames[:, 1, :, :, :] = 255
        video = Video(frames, fps=2, format="gif")
        with PILImage.open(io.BytesIO(video.data)) as clip:
            self.assertEqual(clip.size, (8, 2))
            self.assertEqual(clip.n_frames, 2)
        video.encode(fps=4)
        with PILImage.open(io.BytesIO(video.data)) as clip:
            self.assertEqual(clip.info["duration"], 250)


class PlotContracts(unittest.TestCase):
    @unittest.skipUnless(HAS_STATISTICS, "NumPy/pandas/sklearn are optional statistical dependencies")
    def test_large_curve_stratified_resampling_matches_upstream_algorithm(self):
        import pandas as pd
        from sklearn.utils import resample
        truth = [0, 0, 1, 1]
        scores = [[0.9, 0.1], [0.6, 0.4], [0.65, 0.35], [0.2, 0.8]]
        complete = plot.pr_curve(truth, scores, interp_size=21)
        frame = pd.DataFrame(complete.table.data, columns=complete.table.columns)
        expected = resample(frame, replace=False, n_samples=10, random_state=42, stratify=frame["class"]).sort_values(["precision", "recall", "class"]).values.tolist()
        before = Table.MAX_ROWS
        try:
            Table.MAX_ROWS = 10
            with patch("backprop.termwarn") as terminal_warning:
                result = plot.pr_curve(truth, scores, interp_size=21)
            terminal_warning.assert_called_once_with("Table has a limit of 10 rows. Resampling to fit.")
        finally:
            Table.MAX_ROWS = before
        self.assertEqual(result.table.data, expected)

    @unittest.skipUnless(HAS_STATISTICS, "NumPy/pandas/sklearn are optional statistical dependencies")
    def test_statistics_match_real_sklearn_thresholds_and_numpy_interpolation(self):
        import numpy as np
        from sklearn.metrics import roc_curve, precision_recall_curve
        rng = np.random.default_rng(4)
        for trial in range(8):
            truth = np.arange(90) % 3
            scores = rng.integers(0, 30, (90, 3)) / 30
            expected_roc, expected_pr = [], []
            for cls in range(3):
                fpr, tpr, _ = roc_curve(truth, scores[:, cls], pos_label=cls)
                expected_roc.extend([[cls, round(x, 3), round(y, 3)] for x, y in zip(fpr, tpr)])
                precision, recall, _ = precision_recall_curve(truth, scores[:, cls], pos_label=cls)
                precision = np.maximum.accumulate(precision)[::-1]
                recall = recall[::-1]
                sample = np.linspace(0, 1, 21)[::-1]
                sampled = precision[np.searchsorted(recall, sample, side="left")]
                expected_pr.extend([[cls, round(p, 3), round(r, 3)] for p, r in zip(sampled, sample)])
            self.assertEqual(plot.roc_curve(truth, scores).table.data, expected_roc)
            self.assertEqual(plot.pr_curve(truth, scores).table.data, expected_pr)

    def test_line_stroke_and_spec_contract(self):
        table = Table(columns=["x", "y", "cohort"], data=[[1, 2, "a"]])
        chart = plot.line(table, "x", "y", stroke="cohort", title="Trace", split_table=True)
        chart.set_key("loss")
        self.assertEqual(chart.spec.table_key, "Custom Chart Tables/loss_table")
        self.assertEqual(chart.spec.config_key, ("_wandb", "visualize", "loss"))
        self.assertEqual(encoder().encode(chart)["fields"]["series"], "cohort")
        self.assertEqual(chart.spec.config_value["panel_config"]["fieldSettings"]["stroke"], "cohort")

    def test_line_series_matches_pinned_names_and_truncation(self):
        chart = plot.line_series([0, 1], [[2, 3, 4]], xname="epoch")
        self.assertEqual(chart.table.columns, ["step", "lineKey", "lineVal"])
        self.assertEqual(chart.table.data, [[0, "line_0", 2], [1, "line_0", 3]])
        self.assertEqual(chart.spec.string_fields["xname"], "epoch")

    def test_confusion_matrix_counting_and_official_column_names(self):
        chart = plot.confusion_matrix(y_true=[0, 0, 1], preds=[0, 1, 1], class_names=["a", "b"])
        self.assertEqual(chart.table.columns, ["Actual", "Predicted", "nPredictions"])
        self.assertEqual(chart.table.data, [["a", "a", 1], ["a", "b", 1], ["b", "a", 0], ["b", "b", 1]])

    @unittest.skipUnless(HAS_STATISTICS, "NumPy/pandas/sklearn are optional statistical dependencies")
    def test_roc_pr_ties_endpoints_and_class_selection(self):
        truth = [0, 0, 1, 1]
        scores = [[0.9, 0.1], [0.6, 0.4], [0.65, 0.35], [0.2, 0.8]]
        roc = plot.roc_curve(truth, scores, classes_to_plot=[1], labels=["no", "yes"])
        self.assertEqual(roc.table.data, [["yes", 0.0, 0.0], ["yes", 0.0, 0.5], ["yes", 0.5, 0.5], ["yes", 0.5, 1.0], ["yes", 1.0, 1.0]])
        pr = plot.pr_curve(truth, scores, classes_to_plot=[1], interp_size=3)
        self.assertEqual(pr.table.data, [[1, 0.667, 1.0], [1, 1.0, 0.5], [1, 1.0, 0.0]])
        tied = plot.roc_curve([0, 1], [[0.5, 0.5], [0.5, 0.5]], classes_to_plot=[1])
        self.assertEqual(tied.table.data, [[1, 0.0, 0.0], [1, 1.0, 1.0]])
        if importlib.util.find_spec("pandas") is not None:
            import pandas as pd
            self.assertEqual(plot.roc_curve(truth, pd.DataFrame(scores), classes_to_plot=[1], labels=["no", "yes"]).table.data, roc.table.data)

    def test_unregistered_vega_requires_an_active_store(self):
        with self.assertRaisesRegex(Error, "must call wandb.init"):
            plot.plot_table("custom/spec", Table(columns=[]), {})


if __name__ == "__main__":
    unittest.main()
