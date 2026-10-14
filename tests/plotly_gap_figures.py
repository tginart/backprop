"""Genuine figures shared by isolated encoding probes and browser verification.

Architecture: small deterministic trace inputs -> genuine Plotly Figure -> public
W&B/Backprop wrapper -> immutable bytes or SDK history. Each renderer family has
visible magenta data on a dark canvas so screenshots can prove drawn content.
Main callers: plotly_gap_reference_probe and plotly_gap_replay_fixture. Optional
Plotly/NumPy imports happen only when a caller selects this runtime suite.
Why this exists: hand-written browser JSON does not test Python's binary-array
encoding, and a canvas element alone does not prove a 3D trace actually drew.
"""
from __future__ import annotations

MAGENTA = "#e83fe8"
COLORSCALE = [[0, MAGENTA], [1, MAGENTA]]
POLYGON = {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "area",
    "properties": {}, "geometry": {"type": "Polygon", "coordinates": [
        [[-10, -10], [10, -10], [10, 10], [-10, 10], [-10, -10]]]}}]}


def figures():
    """Build one real figure per registered Plotly 4 family and encoding case.

    Main callers: the two probes. All chart coordinates are local. Maps use an
    empty embedded style, geo uses the existing local topology, and images use
    pixel arrays. No provider, token, external URL or service is required.
    """
    import numpy as np
    import plotly.graph_objects as go

    marker = {"color": MAGENTA, "size": 12}
    line = {"color": MAGENTA, "width": 4}
    bins = [0, 0, 1, 1, 2, 2, 3]
    carpet = {"type": "carpet", "a": [0, 1, 2], "b": [0, 1, 2],
        "y": [[0, 1, 2], [1, 2, 3], [2, 3, 4]],
        "aaxis": {"color": MAGENTA}, "baxis": {"color": MAGENTA}}
    tree = {"labels": ["Root", "A", "B"], "parents": ["", "Root", "Root"],
        "values": [3, 1, 2], "branchvalues": "total", "marker": {"colors": [MAGENTA] * 3}}
    financial = {"x": [1, 2], "open": [2, 3], "high": [4, 5], "low": [1, 2],
        "close": [3, 4], "increasing": {"line": {"color": MAGENTA, "width": 3}, "fillcolor": MAGENTA},
        "decreasing": {"line": {"color": MAGENTA, "width": 3}, "fillcolor": MAGENTA}}
    xyz = np.linspace(-1, 1, 5)
    x, y, z = np.meshgrid(xyz, xyz, xyz, indexing="ij")
    volume = {"x": x.ravel(), "y": y.ravel(), "z": z.ravel(),
        "value": (x*x + y*y + z*z).ravel(), "isomin": .3, "isomax": 1.4,
        "surface": {"count": 3}, "colorscale": COLORSCALE, "showscale": False,
        "caps": {"x": {"show": False}, "y": {"show": False}, "z": {"show": False}}}
    flow = {"x": x.ravel(), "y": y.ravel(), "z": z.ravel(),
        "u": np.ones(x.size), "v": (y*.2).ravel(), "w": (z*.2).ravel(),
        "colorscale": COLORSCALE, "showscale": False}
    specs = {
        "scatter": {"data": [{"type": "scatter", "x": [0, 1, 2], "y": [1, 3, 2], "mode": "lines+markers", "marker": marker, "line": line}]},
        "bar": {"data": [{"type": "bar", "x": ["A", "B"], "y": [2, 4], "marker": {"color": MAGENTA}}]},
        "box": {"data": [{"type": "box", "y": [1, 2, 2, 3, 4], "marker": marker, "line": line, "fillcolor": MAGENTA}]},
        "violin": {"data": [{"type": "violin", "y": [1, 2, 2, 3, 4], "marker": marker, "line": line, "fillcolor": MAGENTA}]},
        "funnel": {"data": [{"type": "funnel", "y": ["A", "B", "C"], "x": [6, 4, 2], "marker": {"color": MAGENTA}}]},
        "waterfall": {"data": [{"type": "waterfall", "x": ["A", "B", "C"], "y": [3, -1, 0], "measure": ["relative", "relative", "total"], "increasing": {"marker": {"color": MAGENTA}}, "decreasing": {"marker": {"color": MAGENTA}}, "totals": {"marker": {"color": MAGENTA}}}]},
        "histogram": {"data": [{"type": "histogram", "x": bins, "marker": {"color": MAGENTA}, "xbins": {"start": -.5, "end": 3.5, "size": 1}}]},
        "histogram2d": {"data": [{"type": "histogram2d", "x": bins, "y": bins, "colorscale": COLORSCALE, "showscale": False}]},
        "histogram2dcontour": {"data": [{"type": "histogram2dcontour", "x": bins, "y": [0, 1, 0, 2, 1, 3, 2], "colorscale": COLORSCALE, "showscale": False}]},
        "heatmap": {"data": [{"type": "heatmap", "z": [[1, 2], [3, 4]], "colorscale": COLORSCALE, "showscale": False}]},
        "contour": {"data": [{"type": "contour", "z": [[1, 2, 3], [3, 1, 4], [2, 4, 1]], "colorscale": COLORSCALE, "showscale": False}]},
        "pie": {"data": [{"type": "pie", "labels": ["A", "B"], "values": [2, 3], "marker": {"colors": [MAGENTA, MAGENTA]}}]},
        "funnelarea": {"data": [{"type": "funnelarea", "labels": ["A", "B"], "values": [3, 2], "marker": {"colors": [MAGENTA, MAGENTA]}}]},
        "sunburst": {"data": [{"type": "sunburst", **tree}]},
        "treemap": {"data": [{"type": "treemap", **tree}]},
        "icicle": {"data": [{"type": "icicle", **tree}]},
        "sankey": {"data": [{"type": "sankey", "node": {"label": ["A", "B", "C"], "color": MAGENTA}, "link": {"source": [0, 0], "target": [1, 2], "value": [2, 3], "color": MAGENTA}}]},
        "parcats": {"data": [{"type": "parcats", "dimensions": [{"label": "A", "values": ["a", "a", "b"]}, {"label": "B", "values": ["x", "y", "x"]}], "line": {"color": MAGENTA}}]},
        "parcoords": {"data": [{"type": "parcoords", "line": {"color": MAGENTA}, "dimensions": [{"label": "A", "values": [0, 1, 2]}, {"label": "B", "values": [2, 0, 1]}]}]},
        "indicator": {"data": [{"type": "indicator", "mode": "number+gauge", "value": 42, "number": {"font": {"color": MAGENTA}}, "gauge": {"axis": {"range": [0, 100]}, "bar": {"color": MAGENTA}}}]},
        "table": {"data": [{"type": "table", "header": {"values": ["A", "B"], "fill": {"color": MAGENTA}}, "cells": {"values": [[1, 2], [3, 4]], "fill": {"color": MAGENTA}}}]},
        "image": {"data": [{"type": "image", "z": [[[232, 63, 232], [232, 63, 232]], [[232, 63, 232], [232, 63, 232]]]}]},
        "ohlc": {"data": [{"type": "ohlc", **{k: v for k, v in financial.items() if k not in {"increasing", "decreasing"}}, "increasing": {"line": line}, "decreasing": {"line": line}}]},
        "candlestick": {"data": [{"type": "candlestick", **financial}]},
        "scatterpolar": {"data": [{"type": "scatterpolar", "r": [1, 2, 3], "theta": [0, 120, 240], "mode": "lines+markers", "marker": marker, "line": line}]},
        "barpolar": {"data": [{"type": "barpolar", "r": [1, 2, 3], "theta": [0, 120, 240], "marker": {"color": MAGENTA}}]},
        "scatterternary": {"data": [{"type": "scatterternary", "a": [1, 2, 3], "b": [3, 2, 1], "c": [2, 2, 2], "mode": "lines+markers", "marker": marker, "line": line}]},
        "scattersmith": {"data": [{"type": "scattersmith", "real": [0, 1, 2], "imag": [0, 1, -.5], "mode": "lines+markers", "marker": marker, "line": line}]},
        "carpet": {"data": [carpet]},
        "scattercarpet": {"data": [carpet, {"type": "scattercarpet", "a": [0, 1, 2], "b": [1, 2, 0], "mode": "lines+markers", "marker": marker, "line": line}]},
        "contourcarpet": {"data": [carpet, {"type": "contourcarpet", "a": [0, 1, 2], "b": [0, 1, 2], "z": [[1, 2, 3], [3, 1, 4], [2, 4, 1]], "colorscale": COLORSCALE, "showscale": False}]},
        "quiver": {"data": [{"type": "quiver", "x": [0, 1, 2], "y": [1, 0, 2], "u": [1, .5, -.5], "v": [.5, 1, 1], "marker": {"color": MAGENTA, "line": {"width": 5}}}]},
        "scattergl": {"data": [{"type": "scattergl", "x": np.arange(64, dtype=np.int32), "y": np.linspace(0, 3, 64, dtype=np.float32), "mode": "lines+markers", "marker": marker, "line": line}]},
        "scatterpolargl": {"data": [{"type": "scatterpolargl", "r": [1, 2, 3], "theta": [0, 120, 240], "mode": "lines+markers", "marker": marker, "line": line}]},
        "splom": {"data": [{"type": "splom", "dimensions": [{"label": "A", "values": [0, 1, 2]}, {"label": "B", "values": [2, 0, 1]}], "marker": marker}]},
        "scatter3d": {"data": [{"type": "scatter3d", "x": [0, 1, 2], "y": [1, 3, 2], "z": [0, 2, 4], "mode": "lines+markers", "marker": marker, "line": line}]},
        "surface": {"data": [{"type": "surface", "z": [[0, 1, 2], [1, 2, 0], [2, 0, 1]], "colorscale": COLORSCALE, "showscale": False}]},
        "mesh3d": {"data": [{"type": "mesh3d", "x": [0, 1, 0, 0], "y": [0, 0, 1, 0], "z": [0, 0, 0, 1], "i": [0, 0, 0, 1], "j": [1, 1, 2, 2], "k": [2, 3, 3, 3], "color": MAGENTA}]},
        "cone": {"data": [{"type": "cone", "x": [0, 1], "y": [0, 1], "z": [0, 1], "u": [1, 1], "v": [1, 0], "w": [1, 1], "colorscale": COLORSCALE, "showscale": False, "sizemode": "absolute", "sizeref": .7}]},
        "streamtube": {"data": [{"type": "streamtube", **flow, "starts": {"x": [-.8, -.8, -.8], "y": [-.5, 0, .5], "z": [-.5, 0, .5]}, "sizeref": .4, "maxdisplayed": 100}]},
        "isosurface": {"data": [{"type": "isosurface", **volume}]},
        "volume": {"data": [{"type": "volume", **volume, "opacity": .4}]},
        "scattergeo": {"data": [{"type": "scattergeo", "lon": [-10, 0, 10], "lat": [0, 10, 0], "mode": "lines+markers", "marker": marker, "line": line}], "layout": {"geo": {"showcoastlines": False, "showland": False, "bgcolor": "#171b20"}}},
        "choropleth": {"data": [{"type": "choropleth", "geojson": POLYGON, "locations": ["area"], "z": [1], "colorscale": COLORSCALE, "showscale": False}], "layout": {"geo": {"fitbounds": "locations", "showcoastlines": False, "showland": False, "bgcolor": "#171b20"}}},
        "scattermap": {"data": [{"type": "scattermap", "lon": [-5, 5], "lat": [0, 5], "mode": "markers", "marker": marker}]},
        "choroplethmap": {"data": [{"type": "choroplethmap", "geojson": POLYGON, "locations": ["area"], "z": [1], "colorscale": COLORSCALE, "showscale": False}]},
        "densitymap": {"data": [{"type": "densitymap", "lon": [-5, 0, 5], "lat": [0, 5, 0], "z": [1, 2, 1], "radius": 40, "colorscale": COLORSCALE, "showscale": False}]},
    }
    style = {"version": 8, "sources": {}, "layers": [{"id": "background", "type": "background", "paint": {"background-color": "#171b20"}}]}
    for name in ["scattermap", "choroplethmap", "densitymap"]:
        specs[name]["layout"] = {"map": {"style": style, "center": {"lon": 0, "lat": 0}, "zoom": 2}}
    # Companion carpets must not supply the test color for a missing overlay.
    # Those two screenshots prove their scatter/contour trace actually drew.
    for name in ["scattercarpet", "contourcarpet"]:
        specs[name]["data"][0] = {**carpet, "aaxis": {"color": "#c5cbd3"}, "baxis": {"color": "#c5cbd3"}}
    for name, spec in specs.items():
        spec["layout"] = {"template": "none", "paper_bgcolor": "#171b20", "plot_bgcolor": "#171b20",
            "font": {"color": "#c5cbd3"}, "height": 320, "width": 440, "showlegend": False,
            "margin": {"l": 45, "r": 20, "t": 20, "b": 40}, **spec.get("layout", {})}
        if name in {"scatter3d", "surface", "mesh3d", "cone", "streamtube", "volume", "isosurface"}:
            spec["layout"]["scene"] = {"xaxis": {"showbackground": False}, "yaxis": {"showbackground": False}, "zaxis": {"showbackground": False}, "bgcolor": "#171b20"}
    out = {name: go.Figure(spec) for name, spec in specs.items()}
    for dtype in ["i1", "u1", "i2", "u2", "i4", "u4", "f4", "f8", "i8", "u8"]:
        out["typed_" + dtype] = go.Figure(data=[go.Scatter(x=np.arange(4, dtype=dtype), y=np.array([1, 3, 2, 4], dtype=dtype), marker=marker, line=line)], layout=specs["scatter"]["layout"])
    out["typed_matrix"] = go.Figure(data=[go.Heatmap(z=np.array([[1, 2], [3, 4]], dtype="f8"), colorscale=[[0, "rgb(0,0,0)"], [1, "rgb(255,255,255)"]], zmin=1, zmax=4, showscale=False)], layout=specs["heatmap"]["layout"])
    out["dates_gaps"] = go.Figure(data=[go.Scatter(x=np.array(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"], dtype="datetime64[D]"), y=np.array([1., np.nan, 3., np.inf]), mode="lines+markers", marker=marker, line=line)], layout=specs["scatter"]["layout"])
    out["animation"] = go.Figure(data=[go.Scatter(x=[0, 1], y=[1, 2], name="Moving", marker=marker, line=line)],
        layout={**specs["scatter"]["layout"], "showlegend": True, "uirevision": "saved-animation", "xaxis": {"range": [-1, 3], "title": "Saved x"}, "yaxis": {"range": [0, 6]},
            "annotations": [{"text": "Saved annotation", "x": .5, "y": .5, "showarrow": False}],
            "sliders": [{"steps": [{"label": "next", "method": "animate", "args": [["next"], {"mode": "immediate", "frame": {"duration": 0}, "transition": {"duration": 0}}]}]}],
            "updatemenus": [{"type": "buttons", "buttons": [{"label": "Play", "method": "animate", "args": [["next"], {"frame": {"duration": 0}, "transition": {"duration": 0}}]}]}]},
        frames=[go.Frame(name="next", traces=[0], data=[go.Scatter(x=[0, 1], y=[4, 5])], layout={"title": {"text": "Animated title"}})])
    return out
