"""Produce actual SDK history for the renderer's complete trace-family matrix.

Architecture: genuine Figures -> Plotly public constructors -> native logging ->
ordinary MemorySink/public history -> browser-ready descriptors and replay events.
Main callers: browser_plotly_gap_qa and test_plotly_gap_replay. These fixtures
exercise the existing store and encoder, not a browser-only JSON approximation.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop as w
from backprop.compatibility import install_wandb_aliases
from plotly_gap_figures import figures, MAGENTA, COLORSCALE, POLYGON


def fixture():
    """Log the matrix at each alias and expose identical replay inputs.

    Main callers: the script entry point and Python test helpers. Native history
    preserves binary descriptors and nonfinite wrappers; public file encoding is
    compared separately because browser-friendly number wrappers are intentional.
    """
    install_wandb_aliases()
    aliases = [getattr(importlib.import_module(module), "Plotly") for module in
        ["wandb", "wandb.data_types", "wandb.sdk.data_types.plotly"]]
    with tempfile.TemporaryDirectory() as directory:
        sink = w.MemorySink()
        with w.Run(id="plotly-gap", entity="tests", project="plotly", dir=directory,
                _sink=sink, _deployment={}, settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True}) as run:
            values = {name: aliases[index % len(aliases)](figure) for index, (name, figure) in enumerate(figures().items())}
            # Plotly Python 7 removed the legacy Mapbox builders. Their existing
            # portable reconstruction contract still needs actual SDK history
            # and the retained 3.6 renderer, independently of upstream encoding.
            legacy = {
                "scattermapbox": {"lon": [-5, 5], "lat": [0, 5], "mode": "markers", "marker": {"color": MAGENTA, "size": 12}},
                "choroplethmapbox": {"geojson": POLYGON, "locations": ["area"], "z": [1], "colorscale": COLORSCALE, "showscale": False},
                "densitymapbox": {"lon": [-5, 0, 5], "lat": [0, 5, 0], "z": [1, 2, 1], "radius": 40, "colorscale": COLORSCALE, "showscale": False},
            }
            for kind, trace in legacy.items():
                values["legacy_" + kind] = w.Plotly({"data": [{"type": kind, **trace}], "layout": {
                    "width": 440, "height": 320, "paper_bgcolor": "#171b20", "font": {"color": "#c5cbd3"}, "showlegend": False,
                    "margin": {"l": 45, "r": 20, "t": 20, "b": 40}, "mapbox": {"center": {"lon": 0, "lat": 0}, "zoom": 2,
                        "style": {"version": 8, "sources": {}, "layers": [{"id": "background", "type": "background", "paint": {"background-color": "#171b20"}}]}}}})
            # These explicit unsupported values enter through Backprop's native
            # dictionary reconstruction contract, not the upstream constructor.
            values["unsupported_trace"] = w.Plotly({"data": [{"type": "future_trace", "x": [0, 1], "y": [1, 2]}]})
            values["blocked_image"] = w.Plotly({"data": [{"type": "image", "source": "ftp://private.invalid/image?token=do-not-display"}]})
            values["invalid_image"] = w.Plotly({"data": [{"type": "image", "source": "data:image/png;base64,aW52YWxpZA=="}]})
            values["blocked_map"] = w.Plotly({"data": [{"type": "scattermap", "lon": [0], "lat": [0]}], "layout": {"map": {"style": {"version": 8, "sources": {"remote": {"type": "raster", "tiles": ["https://blocked.invalid/{z}/{x}/{y}.png?token=do-not-display"]}}, "layers": []}}}})
            run.log(values)
        api = w.Api._from_store(sink, entity="tests", project="plotly")
        history = list(api.run(run.id).scan_history())
        return {"events": sink.events, "history": history,
            "values": {key: value for key, value in history[0].items() if not key.startswith("_")},
            "figure_names": sorted(figures()), "native_legacy_names": sorted("legacy_" + name for name in legacy)}


if __name__ == "__main__":
    print(json.dumps(fixture(), sort_keys=True))
