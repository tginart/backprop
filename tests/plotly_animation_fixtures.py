"""Genuine SDK inputs for mixed, layout, easing and retained-renderer controls.

Architecture: genuine Figures -> independent pinned encoding -> native history
-> viewer versus direct pinned renderer. Legacy Mapbox builders were removed by
installed Plotly Python, so their selected native reconstruction contract is
explicitly separate from the genuine Figure import/encoding comparison.
Main callers: plotly_animation_probe and plotly_animation_browser_qa.
"""
from __future__ import annotations
import copy
import base64
import importlib
from pathlib import Path
import sys
import tempfile
import threading
import struct
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from plotly_gap_options_figures import option_figures, frame_value, CYAN
from plotly_gap_figures import figures, MAGENTA, POLYGON
from plotly_animation_selection_figures import selection_figures
from plotly_animation_branch_figures import ALL_EASINGS, TIMED_FAMILIES, branch_figures


def button(label, selection, *, duration=0, easing="linear", redraw=False, mode="immediate"):
    return {"label": label, "method": "animate", "args": [selection, {"mode": mode,
        "frame": {"duration": duration, "redraw": redraw}, "transition": {"duration": duration, "easing": easing}}]}


def genuine_figures(resource_url="https://capture.invalid/inline.png"):
    """Build finite saved-control cases without fabricating browser trace JSON.

    Main callers: isolated encoding probe and native fixture publication. Fixed
    axes make intermediate drawn geometry measurable independently of fullData,
    which Plotly changes to the final values before its SVG transition finishes.
    """
    import plotly.graph_objects as go
    base = figures()
    out = {}
    mixed = go.Figure(option_figures()["mixed_scenes"])
    next_data = [frame_value(trace.to_plotly_json()) for trace in mixed.data]
    next_data[0]["z"] = [1, 3, 5]
    next_data[3]["y"] = [5, 1]
    next_data[4]["r"] = [3, 2, 1]
    mixed.frames = [go.Frame(name="base", traces=list(range(5)), data=[trace.to_plotly_json() for trace in mixed.data], layout={"title": {"text": "Mixed base"}}),
        go.Frame(name="next", traces=list(range(5)), data=next_data, layout={"title": {"text": "Mixed next"}, "annotations": [{"text": "Saved mixed annotation", "x": .5, "y": .5, "showarrow": False}]})]
    mixed.update_layout(updatemenus=[{"type": "buttons", "buttons": [button("Next", ["next"]), button("Back", ["base"])]}],
        sliders=[{"steps": [button("Base", ["base"]), button("Frame", ["next"])]}])
    out["mixed_animation"] = mixed

    layout = go.Figure(base["bar"])
    initial = {"title": {"text": "Layout base"}, "paper_bgcolor": "#171b20", "annotations": [], "shapes": [],
        "xaxis": {"range": [-.5, 1.5]}, "yaxis": {"range": [0, 8]}, "font": {"color": "#c5cbd3"}, "margin": {"l": 45, "r": 20, "t": 70, "b": 40}}
    layout.update_layout(initial)
    changed = {"title": {"text": "Layout next"}, "paper_bgcolor": "#253141", "annotations": [{"text": "New saved annotation", "x": .5, "y": .85, "xref": "paper", "yref": "paper", "showarrow": False}],
        "shapes": [{"type": "line", "x0": 0, "x1": 1, "y0": 6, "y1": 6, "line": {"color": "#e8e8e8", "width": 3}}], "font": {"color": "#f0f0f0"}, "margin": {"l": 65, "r": 30, "t": 75, "b": 55}}
    layout.frames = [go.Frame(name="base", group="base", data=[base["bar"].data[0]], layout=initial),
        go.Frame(name="next", group="next", data=[go.Bar(x=["A", "B"], y=[5, 1], marker={"color": CYAN})], layout=changed)]
    layout.update_layout(updatemenus=[{"type": "buttons", "buttons": [button("Next", "next", duration=350), button("Back", ["base"], duration=350)]}],
        sliders=[{"steps": [button("Base", ["base"]), button("Frame", ["next"])]}])
    out["layout_animation"] = layout

    for family in TIMED_FAMILIES:
        for easing in ALL_EASINGS:
            fig = go.Figure(base[family])
            target = fig.data[0].to_plotly_json()
            if family in {"scatter", "bar"}:
                fig.data[0].y = [1, 2, 3] if family == "scatter" else [1, 2]
                target["y"] = [7, 8, 9] if family == "scatter" else [7, 8]
                fig.update_layout(yaxis={"range": [0, 10]})
            elif family == "quiver":
                fig.data[0].u = [1, .5, -.5]
                target["u"] = [2, 1.5, .5]
                fig.update_layout(xaxis={"range": [-1, 5]}, yaxis={"range": [-1, 4]})
            elif family == "indicator":
                fig.data[0].value = 20
                target["value"] = 80
            else:
                target["level"] = "A"
            if family == "scatter":
                fig.data[0].line.simplify = False
                target["line"] = {"simplify": False, "color": MAGENTA, "width": 4}
            fig.frames = [go.Frame(name="next", traces=[0], data=[target])]
            fig.update_layout(margin={"t": 55}, updatemenus=[{"type": "buttons", "buttons": [button("Animate", ["next"], duration=900, easing=easing)]}])
            out["interpolate_" + family + "_" + easing] = fig

    queue = go.Figure(base["bar"])
    queue.update_layout(title={"text": "Queue base"}, margin={"t": 70}, yaxis={"range": [0, 10]})
    queue.frames = [go.Frame(name=name, group="sequence", data=[go.Bar(x=["A", "B"], y=[value, 1], marker={"color": CYAN})], layout={"title": {"text": "Queue " + name}}) for name, value in [("a", 3), ("b", 5), ("c", 7)]]
    # Real pointer delivery can take longer than a short first frame in CI.
    # Keep the requested interruption during frame a observable, while native
    # timing assertions still use the saved durations and unchanged transition
    # array/default behavior. This is fixture timing, not a renderer workaround.
    play = button("Play", "sequence", duration=1000, mode="afterall")
    play["args"][1]["frame"] = [{"duration": 1000, "redraw": False}, {"duration": 360, "redraw": False}]
    play["args"][1]["transition"] = [{"duration": 120, "easing": "linear"}, {"duration": 180, "easing": "quad-in"}]
    queue.update_layout(updatemenus=[{"type": "buttons", "buttons": [play, button("Tail", ["c"], duration=240, mode="afterall"), button("Next queued", ["c"], duration=240, mode="next"), button("Immediate", ["c"], duration=240), button("Stop", [], duration=0)]}])
    out["queue_animation"] = queue
    inline = go.Figure(base["bar"])
    next_frame = {"traces": [0], "data": [{"type": "bar", "x": ["A", "B"], "y": [5, 1], "marker": {"color": CYAN}}],
        "layout": {"title": {"text": "Inline next"}, "images": [{"source": resource_url, "visible": True, "xref": "paper", "yref": "paper", "x": .55, "y": .8, "sizex": .3, "sizey": .3}]}}
    base_frame = {"traces": [0], "data": [base["bar"].data[0].to_plotly_json()], "layout": {"title": {"text": "Inline base"}, "images": [{"visible": False}]}}
    toggle = button("Toggle", [next_frame], duration=150)
    toggle["args2"] = button("Back", [base_frame], duration=150)["args"]
    inline.update_layout(title={"text": "Inline base"}, margin={"t": 70}, updatemenus=[{"type": "buttons", "active": -1,
        "buttons": [toggle, {**button("Disabled execution", [next_frame]), "execute": False}]}],
        sliders=[{"steps": [button("Base", [base_frame]), button("Frame", [next_frame])]}])
    out["inline_resource_animation"] = inline
    forbidden = go.Figure(base["bar"])
    forbidden.update_layout(updatemenus=[{"type": "buttons", "buttons": [button("Blocked", [{"layout": {"images": [{"source": "ftp://blocked.invalid/private?secret=123"}]}}])]}])
    out["inline_resource_blocked"] = forbidden
    # Negative requests still serialize, but must fail explicitly in the viewer.
    missing = go.Figure(base["bar"])
    missing.update_layout(updatemenus=[{"type": "buttons", "buttons": [button("Missing", ["absent"])]}])
    out["missing_frame"] = missing
    out.update(selection_figures())
    out.update(branch_figures())
    return out


def legacy_values():
    """Build the retained native Mapbox contract with real saved controls.

    Main caller: native_fixture. Embedded blank styles/GeoJSON require no tokens,
    tiles, network resources or historical Python installation.
    """
    style = {"version": 8, "sources": {}, "layers": [{"id": "background", "type": "background", "paint": {"background-color": "#171b20"}}]}
    traces = {
        "scattermapbox": {"type": "scattermapbox", "lon": [-5, 5], "lat": [0, 5], "mode": "markers", "marker": {"color": MAGENTA, "size": 14}},
        "densitymapbox": {"type": "densitymapbox", "lon": [-5, 0, 5], "lat": [0, 5, 0], "z": [1, 2, 1], "radius": 40, "colorscale": [[0, MAGENTA], [1, MAGENTA]], "showscale": False},
        "choroplethmapbox": {"type": "choroplethmapbox", "geojson": POLYGON, "locations": ["area"], "z": [1], "colorscale": [[0, MAGENTA], [1, MAGENTA]], "showscale": False},
    }
    out = {}
    for name, trace in traces.items():
        layout = {"template": "none", "width": 440, "height": 340, "paper_bgcolor": "#171b20", "font": {"color": "#c5cbd3"}, "title": {"text": "Legacy base"}, "margin": {"l": 25, "r": 25, "t": 65, "b": 30},
            "mapbox": {"style": copy.deepcopy(style), "center": {"lon": 0, "lat": 0}, "zoom": 2, "bearing": 0, "pitch": 0}, "updatemenus": [{"type": "buttons", "buttons": [button("Next", ["next"]), button("Back", ["base"])]}],
            "sliders": [{"steps": [button("Base", ["base"]), button("Frame", ["next"])]}]}
        out["legacy_" + name] = {"data": [trace], "layout": layout, "frames": [{"name": "base", "traces": [0], "data": [copy.deepcopy(trace)], "layout": {"title": {"text": "Legacy base"}}},
            {"name": "next", "traces": [0], "data": [frame_value(trace)], "layout": {"title": {"text": "Legacy next"}}}]}
    return out


def native_fixture():
    """Publish real Run/MemorySink history for both explicitly distinct inputs.

    Main caller: browser driver. Every genuine Figure uses a public import and
    SDK serialization; the browser receives those exact native descriptors.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
    aliases = [getattr(importlib.import_module(module), "Plotly") for module in ["wandb", "wandb.data_types", "wandb.sdk.data_types.plotly"]]
    # A finite HTTP source is shut down before the browser runs. Rendering must
    # therefore use the actual captured MemorySink bytes, never the original URL.
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\0" + bytes([0, 232, 232]) * 2) * 2)) + chunk(b"IEND", b"")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(png)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            sink = w.MemorySink()
            with w.Run(id="plotly-animation", entity="tests", project="plotly", dir=directory, _sink=sink, _deployment={}, settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True}) as run:
                values = {name: aliases[index % 3](fig) for index, (name, fig) in enumerate(genuine_figures(f"http://127.0.0.1:{server.server_port}/inline.png").items())}
                values.update({name: w.Plotly(value) for name, value in legacy_values().items()})
                run.log(values)
                updates = selection_figures()
                for suffix in ["before", "after"]:
                    run.log({stem + "_before": aliases[index](updates[stem + "_" + suffix])
                        for index, stem in enumerate(["react_layout_first", "react_traces_first"])})
                branches = branch_figures()
                for index, suffix in enumerate(["before", "after", "before", "after"]):
                    run.log({"retained_frames_before": aliases[index % 3](branches["retained_frames_" + suffix])})
            rows = list(w.Api._from_store(sink, entity="tests", project="plotly").run(run.id).scan_history())
            row = rows[0]
            history_updates = {stem + "_before": [history[stem + "_before"] for history in rows[1:] if stem + "_before" in history]
                for stem in ["react_layout_first", "react_traces_first"]}
            history_updates["retained_frames_before"] = [history["retained_frames_before"] for history in rows[1:] if "retained_frames_before" in history]
            media = {resource["file"]["path"]: {"mime_type": resource["file"]["mime_type"], "base64": base64.b64encode(sink.download_file(resource["file"])).decode()}
                for value in row.values() if isinstance(value, dict) for resource in value.get("resources", [])}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return {"values": {key: value for key, value in row.items() if not key.startswith("_")}, "history_updates": history_updates, "media": media,
        "coverage": {"easings": ALL_EASINGS, "timed_families": TIMED_FAMILIES, "attribute_cases": [name for name in branches if name.startswith("branch_") and name != "branch_quiver_implicit_xy"]},
        "errors": ["missing_frame", "inline_resource_blocked", "missing_inheritance", "cyclic_inheritance", "branch_quiver_implicit_xy"]}


if __name__ == "__main__":
    import json
    print(json.dumps(native_fixture(), sort_keys=True))
