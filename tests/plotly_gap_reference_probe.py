"""Compare real Plotly encoding through every captured public import in isolation.

Architecture: pinned installed W&B or local SDK -> independent import aliases ->
genuine Figures -> frozen public file bytes and constructor errors. Figure bytes
are independent of service publication and are compared before browser rendering.
Main caller: test_optional_plotly_gap_encoding. No W&B service is contacted.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sys
import warnings

from plotly_gap_figures import figures

REFERENCE = sys.argv[1] == "reference"
if REFERENCE:
    import wandb as w
    assert w.__version__ == "0.30.0"
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def error(call):
    """Keep constructor rejection separate from encoding equality.

    Main caller: the alias loop. Error classes are the portable contract; the
    existing image-conversion error has provider-specific guidance in its text.
    """
    try:
        call()
    except Exception as caught:
        return type(caught).__name__
    raise AssertionError("Invalid Plotly constructor input was accepted")


def file_observation(value):
    payload = Path(value._path).read_bytes()
    return {"bytes": payload.decode("utf-8"), "sha256": hashlib.sha256(payload).hexdigest(),
        "file_is_set": value.file_is_set(), "is_bound": value.is_bound()}


fixture = figures()
out = {"aliases": {}, "figure_names": sorted(fixture)}
if REFERENCE:
    out["source_sha256"] = hashlib.sha256(Path(inspect.getfile(w.Plotly)).read_bytes()).hexdigest()
for module in ["wandb", "wandb.data_types", "wandb.sdk.data_types.plotly"]:
    cls = getattr(importlib.import_module(module), "Plotly")
    encoded = {name: file_observation(cls(figure)) for name, figure in fixture.items()}
    # A real Figure mutation after construction cannot rewrite the saved file.
    moving = fixture["animation"]
    saved = cls(moving)
    before = Path(saved._path).read_bytes()
    moving.data[0].name = "caller mutation"
    frozen = Path(saved._path).read_bytes() == before
    moving.data[0].name = "Moving"
    out["aliases"][module + ".Plotly"] = {"same_class": cls is w.Plotly,
        "encoding": encoded, "frozen": frozen,
        "invalid": [error(lambda value=value: cls(value)) for value in [None, 1, [], object()]]}

    # Use genuine installed matplotlib conversion too, including the image
    # boundary. Close all figures so optional dependencies remain well behaved.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fig, ax = plt.subplots(figsize=(4, 3))
        ax.plot([0, 1, 2], [1, 3, 2], color="magenta")
        ax.set_title("Converted matplotlib")
        out["aliases"][module + ".Plotly"]["matplotlib"] = file_observation(cls(fig))
        plt.close(fig)
        fig, ax = plt.subplots()
        ax.imshow([[1, 2], [3, 4]])
        out["aliases"][module + ".Plotly"]["matplotlib_image_error"] = error(lambda: cls(fig))
        out["aliases"][module + ".Plotly"]["matplotlib_image_media"] = type(cls.make_plot_media(fig)).__name__
        plt.close(fig)
print(json.dumps(out, sort_keys=True))
