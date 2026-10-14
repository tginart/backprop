"""Check Plotly serialization against unchanged pinned W&B wrapper bodies.

Architecture: hash-checked source -> isolated official wrapper -> compare stored
JSON with Backprop -> verify native history/artifact roundtrips. Figure fixtures
exercise the wrapper boundary; a separately gated test uses genuine Plotly.
Main callers: unittest discovery. No optional packages are installed by tests.
"""
from __future__ import annotations

import ast
from collections.abc import Sequence
import codecs
from datetime import date, datetime, timezone
import enum
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

import backprop
from backprop.media import Media
from backprop.transport import MemorySink

ROOT = Path(__file__).resolve().parents[1] / ".reference/wandb/source"
SOURCES = {
    "wandb/sdk/data_types/plotly.py": "d50a5d0c611821910864b43da66e29c38328ee28becbd86beff63d20429785f3",
    "wandb/sdk/data_types/base_types/media.py": "9c2ea72e84eb2b07c91ddaa4e5ca94e70aff0c25ede272bf5ccc257efe61f856",
    "wandb/util.py": "414dd36708ea72b8429074b556f61962c03ce06bb7268570467051405bcfcb27",
}
HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HAS_PLOTLY = importlib.util.find_spec("plotly") is not None
HAS_REFERENCE = all((ROOT / name).is_file() for name in SOURCES)


def load_nodes(name, symbols, namespace):
    source = (ROOT / name).read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCES[name]:
        raise AssertionError("Plotly reference differs from pinned W&B 0.30.0")
    nodes = [node for node in ast.parse(source).body if
             isinstance(node, ast.ImportFrom) and node.module == "__future__" or
             isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in symbols]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / name), "exec"), namespace)


def official_plotly(directory):
    """Supply temporary storage and imports around exact serialization code."""
    import numpy as np
    typename = lambda value: type(value).__module__ + "." + type(value).__name__
    namespace = {"json": json, "np": np, "math": math, "enum": enum,
                 "datetime": datetime, "date": date, "getsizeof": sys.getsizeof,
                 "VALUE_BYTES_LIMIT": 100000000, "TYPE_CHECKING": False,
                 "get_full_typename": typename,
                 "is_numpy_array": lambda value: isinstance(value, np.ndarray),
                 "wandb": SimpleNamespace(termwarn=lambda *args: None)}
    load_nodes("wandb/util.py", {"_numpy_generic_convert", "_sanitize_numpy_keys", "json_friendly", "WandBJSONEncoder",
        "is_tf_eager_tensor_typename", "is_tf_tensor_typename", "is_pytorch_tensor_typename",
        "is_fastai_tensor_typename", "is_jax_tensor_typename", "get_jax_tensor"}, namespace)
    encoder = namespace["WandBJSONEncoder"]
    # W&B 0.30.0 uses pydantic-core first; stdlib-only execution would miss
    # enum/date coercion and compact file-byte behavior in the actual wrapper.
    import pydantic_core
    def dump(value, stream):
        try:
            encoded = pydantic_core.to_json(value, inf_nan_mode="constants", fallback=encoder().default).decode()
        except Exception:
            encoded = json.dumps(value, cls=encoder)
        stream.write(encoded)
    utility = SimpleNamespace(is_numpy_array=namespace["is_numpy_array"],
        get_full_typename=typename, is_plotly_figure_typename=lambda name: name.startswith("plotly.") and name.endswith(".Figure"),
        is_matplotlib_typename=lambda name: name.startswith("matplotlib."),
        json_dump_safer=dump)
    normalize = {"util": utility, "Sequence": Sequence, "Media": Media, "TYPE_CHECKING": False}
    load_nodes("wandb/sdk/data_types/base_types/media.py", {"_numpy_arrays_to_lists"}, normalize)
    class ReferenceMedia:
        def _set_file(self, path, **kwargs):
            self.spec = json.loads(Path(path).read_text())
    wrapper = {"Media": ReferenceMedia, "util": utility, "os": os, "codecs": codecs,
               "MEDIA_TMP": SimpleNamespace(name=str(directory)),
               "runid": SimpleNamespace(generate_id=lambda: uuid.uuid4().hex),
               "_numpy_arrays_to_lists": normalize["_numpy_arrays_to_lists"]}
    load_nodes("wandb/sdk/data_types/plotly.py", {"Plotly"}, wrapper)
    return wrapper["Plotly"]


def figure_fixture(spec):
    def forbidden(self):
        raise AssertionError("Pinned W&B serializes to_plotly_json, not to_json")
    cls = type("Figure", (), {"__module__": "plotly.graph_objs._figure",
                             "to_plotly_json": lambda self: spec, "to_json": forbidden})
    return cls()


@unittest.skipUnless(HAS_NUMPY and HAS_REFERENCE, "Pinned source and NumPy are required for Plotly comparisons")
class PlotlyWrapperContracts(unittest.TestCase):
    def test_nested_arrays_dates_media_and_scalars_match_exact_pinned_wrapper(self):
        import numpy as np
        class Color(enum.Enum):
            BLUE = "blue"
        spec = {"data": [{"type": "scatter", "x": np.arange(64), "y": np.linspace(1, 4, 64),
                         "customdata": [np.array(3), np.array([[1, 2], [3, 4]])]}],
                "layout": {"title": "Arrays", "meta": [date(2026, 1, 2), datetime(2026, 1, 2, tzinfo=timezone.utc),
                    np.int32(7), np.float32(1.5), np.longdouble(1.25), np.datetime64("2026-01-02"),
                    Color.BLUE, {2, 3}, b"ab", backprop.Html("<p>media</p>")]}}
        with tempfile.TemporaryDirectory() as root:
            expected = official_plotly(root)(figure_fixture(spec)).spec
            actual = backprop.Plotly(figure_fixture(spec))
            self.assertEqual(actual.spec, expected)
            self.assertEqual(len(actual.spec["data"][0]["x"]), 64)
            self.assertEqual(actual.spec["data"][0]["customdata"][0], [3])
            spec["data"][0]["x"][0] = 999
            self.assertEqual(actual.spec["data"][0]["x"][0], 0)

    def test_invalid_top_level_and_payload_values_fail_like_pinned(self):
        with tempfile.TemporaryDirectory() as root:
            reference = official_plotly(root)
            for value in (None, 1, object(), figure_fixture({"data": [object()]})):
                errors = []
                for factory in (reference, backprop.Plotly):
                    try:
                        factory(value)
                    except Exception as error:
                        errors.append((type(error), str(error)))
                self.assertEqual(len(errors), 2)
                self.assertEqual(errors[0], errors[1])

    def test_long_coordinates_stay_arrays_through_history_and_artifacts(self):
        import numpy as np
        spec = {"data": [{"type": "scatter", "x": np.arange(64), "y": np.arange(64) ** 2}]}
        value = backprop.Plotly(figure_fixture(spec))
        with tempfile.TemporaryDirectory() as root:
            sink = MemorySink()
            with backprop.Run(project="plotly", entity="tests", id="arrays", dir=root,
                    settings={"console": "off", "silent": True, "disable_git": True}, _sink=sink, _deployment={}) as run:
                run.log({"pending": 1}, commit=False)
                with self.assertRaises(TypeError):
                    run.log({"bad": figure_fixture({"data": [object()]})})
                self.assertEqual(run.step, 0)
                run.log({"bare": figure_fixture(spec), "wrapped": value})
                artifact = backprop.Artifact("coordinates", "plots")
                # Pinned Artifact.add excludes Plotly as a top-level object;
                # supported Table cells can retain its native plot descriptor.
                with self.assertRaises(TypeError):
                    artifact.add(value, "chart")
                artifact.add(backprop.Table(columns=["chart"], data=[[value]]), "charts")
                run.log_artifact(artifact)
            api = backprop.Api._from_store(sink, entity="tests", project="plotly")
            row = list(api.run("arrays").scan_history())[0]
            self.assertEqual(row["pending"], 1)
            self.assertEqual(row["bare"]["spec"], value.spec)
            self.assertEqual(row["wrapped"]["spec"], value.spec)
            self.assertEqual(api.artifact(artifact.qualified_name).get("charts").data[0][0].spec, value.spec)

    @unittest.skipUnless(importlib.util.find_spec("torch"), "Genuine PyTorch runtime is unavailable")
    def test_tensor_metadata_matches_pinned_values_and_large_tensor_error(self):
        import torch
        with tempfile.TemporaryDirectory() as root:
            reference = official_plotly(root)
            for tensor in (torch.tensor(2), torch.tensor([1, 2]), torch.tensor([1.5, 2.5], requires_grad=True),
                           torch.tensor([True, False]), torch.arange(64)):
                with self.subTest(shape=list(tensor.shape), dtype=str(tensor.dtype)):
                    outcomes = []
                    for factory in (reference, backprop.Plotly):
                        try:
                            result = factory(figure_fixture({"layout": {"meta": tensor}})).spec
                        except Exception as error:
                            outcomes.append(("error", type(error), str(error)))
                        else:
                            outcomes.append(("value", result))
                    self.assertEqual(outcomes[0], outcomes[1])

    @unittest.skipUnless(HAS_PLOTLY, "Genuine Plotly runtime is unavailable")
    def test_genuine_plotly_figure_matches_pinned_wrapper(self):
        import numpy as np
        import plotly.graph_objects as go
        figure = go.Figure(go.Scatter(x=np.arange(64), y=np.arange(64) ** 2))
        figure.update_layout(title="Real Plotly", xaxis_type="log")
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(backprop.Plotly(figure).spec, official_plotly(root)(figure).spec)


if __name__ == "__main__":
    unittest.main()
