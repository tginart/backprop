"""Publish genuine native SDK descriptors for the concrete Plotly option gaps.

Architecture: real option Figures -> each public Plotly import -> ordinary native
Run/MemorySink history -> existing browser viewer. Main caller: local options QA.
Error fixtures use native reconstruction deliberately; they test visible policy
and unsupported-family failures rather than the upstream Figure constructor.
"""
import importlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop as w
from backprop.compatibility import install_wandb_aliases
from plotly_gap_options_figures import option_figures


def fixture():
    """Return real history and the finite error branches owned by the viewer.

    Main caller: the script entry point. Original text/resources remain present
    in SDK history; browser preparation must report its presentation restrictions.
    """
    install_wandb_aliases()
    aliases = [getattr(importlib.import_module(module), "Plotly") for module in
        ["wandb", "wandb.data_types", "wandb.sdk.data_types.plotly"]]
    with tempfile.TemporaryDirectory() as directory:
        sink = w.MemorySink()
        with w.Run(id="plotly-options", entity="tests", project="plotly", dir=directory, _sink=sink, _deployment={},
                settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True}) as run:
            values = {name: aliases[index % 3](figure) for index, (name, figure) in enumerate(option_figures().items())}
            errors = {
                "bad_frame_type": {"data": [{"type": "bar", "x": [1], "y": [2]}], "frames": [{"name": "bad", "data": [{"type": "future_trace"}]}]},
                "bad_template_type": {"data": [{"x": [1], "y": [2]}], "layout": {"template": {"data": {"future_trace": [{}]}}}},
                "bad_frame_mapping": {"data": [{"type": "bar", "x": [1], "y": [2]}], "frames": [{"name": "bad", "traces": [999], "data": [{"type": "bar", "y": [3]}]}]},
                "frame_resource": {"data": [{"type": "image", "z": [[[1, 2, 3]]]}], "frames": [{"name": "bad", "traces": [0], "data": [{"source": "ftp://private.invalid/frame?token=secret"}]}]},
                "template_resource": {"data": [{"type": "image"}], "layout": {"template": {"data": {"image": [{"source": "ftp://private.invalid/template?token=secret"}]}}}},
            }
            values.update({name: w.Plotly(spec) for name, spec in errors.items()})
            run.log(values)
        row = list(w.Api._from_store(sink, entity="tests", project="plotly").run(run.id).scan_history())[0]
        return {"values": {key: value for key, value in row.items() if not key.startswith("_")}, "errors": sorted(errors)}


if __name__ == "__main__":
    print(json.dumps(fixture(), sort_keys=True))
