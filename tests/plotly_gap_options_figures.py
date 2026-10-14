"""Finite renderer-sensitive Figure branches for the next Plotly parity pass.

Architecture: the established trace-family fixtures -> one data/style frame per
family -> schema-declared interpolated branches -> mixed scene/subplot layouts.
Main callers: the isolated options probe and actual SDK options replay fixture.
Why this exists: frame redraw and smooth transition are different lifecycle
stages. Every family needs a final frame outcome, while interpolation is limited
to the attributes the established renderer itself declares as animatable.
"""
from __future__ import annotations

import copy

from plotly_gap_figures import figures, MAGENTA

CYAN = "#14dcdc"
TIMED_FAMILIES = {"scatter", "bar", "sunburst", "treemap", "icicle", "indicator", "quiver"}


def frame_value(value):
    """Change only the fixture's data color, including explicit RGB pixels.

    Main caller: option_figures. Gray companion carpets/layouts stay gray, so a
    screenshot's cyan pixels belong to the frame trace rather than its backdrop.
    """
    if isinstance(value, dict):
        return {key: frame_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if list(value) == [232, 63, 232]:
            return [20, 220, 220]
        return [frame_value(item) for item in value]
    if isinstance(value, str) and value == MAGENTA:
        return CYAN
    return copy.deepcopy(value)


def option_figures():
    """Build real Figures for every redraw family and concrete option branch.

    Main callers: the two probes. Frames explicitly remap traces, set a visible
    title and update real geometry when that family has a numerical data field.
    Mixed scenes have independent cameras and coexist with 2D/polar domains.
    """
    import numpy as np
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    base = figures()
    names = [name for name in base if not name.startswith("typed_") and name not in {"animation", "dates_gaps"}]
    fields = {"scatter": "y", "bar": "y", "box": "y", "violin": "y", "funnel": "x", "waterfall": "y",
        "histogram": "x", "histogram2d": "x", "histogram2dcontour": "x", "heatmap": "z", "contour": "z",
        "scatterpolar": "r", "scatterpolargl": "r", "barpolar": "r", "scatterternary": "a", "scattersmith": "imag",
        "carpet": "y", "scattercarpet": "a", "contourcarpet": "z", "quiver": "u", "scattergl": "y",
        "scatter3d": "z", "surface": "z", "mesh3d": "z", "cone": "u", "streamtube": "u", "isosurface": "value", "volume": "value",
        "scattergeo": "lon", "scattermap": "lon", "densitymap": "lon", "ohlc": "close", "candlestick": "close"}
    out = {}
    for name in names:
        figure = go.Figure(base[name])
        traces = [frame_value(trace.to_plotly_json()) for trace in figure.data]
        target = traces[-1]
        field = fields.get(name)
        if field:
            target[field] = np.asarray(target[field], dtype=float) + .25
        if name in {"sunburst", "treemap", "icicle"}:
            target["level"] = "A"
        if name in {"pie", "funnelarea"}:
            target["values"] = [4, 1]
        if name == "indicator":
            target["value"] = 57
        if name == "sankey":
            target["link"]["value"] = [4, 1]
        if name == "parcoords":
            target["dimensions"][0]["values"] = [2, 1, 0]
        if name == "parcats":
            target["dimensions"][0]["values"] = ["b", "a", "a"]
        if name == "table":
            target["cells"]["values"] = [[11, 12], [13, 14]]
        figure.frames = [go.Frame(name="base", traces=list(range(len(traces))), data=[trace.to_plotly_json() for trace in figure.data], layout={"title": {"text": "Base " + name}}),
            go.Frame(name="next", traces=list(range(len(traces))), data=traces, layout={"title": {"text": "Frame " + name}})]
        figure.update_layout(margin={"t": 70}, updatemenus=[{"type": "buttons", "buttons": [
            {"label": "Next", "method": "animate", "args": [["next"], {"mode": "immediate", "frame": {"duration": 0, "redraw": True}, "transition": {"duration": 0}}]},
            {"label": "Back", "method": "animate", "args": [["base"], {"mode": "immediate", "frame": {"duration": 0, "redraw": True}, "transition": {"duration": 0}}]},
        ]}])
        if name in TIMED_FAMILIES:
            figure.frames += (go.Frame(name="smooth", traces=list(range(len(traces))), data=traces),)
            figure.layout.updatemenus[0].buttons += (go.layout.updatemenu.Button(label="Smooth", method="animate", args=[["smooth"], {
                "mode": "immediate", "frame": {"duration": 300, "redraw": False}, "transition": {"duration": 240, "easing": "linear"}}]),)
        out["frame_" + name] = figure

    mixed = make_subplots(rows=2, cols=2, specs=[[{"type": "scene"}, {"type": "scene"}], [{"type": "xy"}, {"type": "polar"}]],
        row_heights=[.65, .35], horizontal_spacing=.1, vertical_spacing=.12)
    mixed.add_trace(base["scatter3d"].data[0], row=1, col=1)
    mixed.add_trace(base["surface"].data[0], row=1, col=1)
    mixed.add_trace(base["mesh3d"].data[0], row=1, col=2)
    mixed.add_trace(base["bar"].data[0], row=2, col=1)
    mixed.add_trace(base["scatterpolar"].data[0], row=2, col=2)
    mixed.update_layout(template="none", width=760, height=620, paper_bgcolor="#171b20", plot_bgcolor="#171b20", font={"color": "#c5cbd3"}, showlegend=False,
        uirevision="mixed", margin={"l": 45, "r": 25, "t": 60, "b": 45},
        scene={"camera": {"eye": {"x": 1.3, "y": 1.7, "z": 1.1}}, "bgcolor": "#171b20"},
        scene2={"camera": {"eye": {"x": -1.5, "y": 1.3, "z": 1.2}}, "bgcolor": "#171b20"},
        xaxis={"range": [-.5, 1.5]}, yaxis={"range": [0, 6]}, polar={"bgcolor": "#171b20"})
    out["mixed_scenes"] = mixed
    responsive = go.Figure(mixed)
    responsive.layout.width = None
    responsive.layout.height = 520
    out["mixed_responsive"] = responsive
    # Genuine source metadata is retained; only browser presentation strips
    # chart anchors. Saved templates override defaults, including explicit light.
    options = go.Figure(go.Scatter(x=[1, 10, 100], y=[2, 4, 8], mode="lines+markers",
        marker={"color": MAGENTA, "size": 14}, line={"color": MAGENTA, "width": 4},
        text=["<a href='https://blocked.invalid/?token=hidden'>Saved link</a>", "B", "C"], hovertemplate="%{text}<extra></extra>"),
        layout={"width": 560, "height": 360, "xaxis": {"type": "log", "range": [0, 2], "title": {"text": "Log x"}},
            "yaxis": {"range": [0, 10]}, "title": {"text": "<a href='javascript:alert(1)'>Saved title</a>"},
            "template": {"layout": {"paper_bgcolor": "#fff", "plot_bgcolor": "#fff", "font": {"color": "#111"}, "margin": {"l": 70, "r": 40, "t": 60, "b": 60}}}})
    out["explicit_template_log"] = options
    # Exercise named base-frame inheritance and group selection with partial
    # trace updates. The renderer must merge the selected base, not lose data.
    inherited = go.Figure(base["bar"])
    inherited.frames = [go.Frame(name="base", group="base-group", data=[go.Bar(x=["A", "B"], y=[2, 4], marker={"color": MAGENTA})], traces=[0]),
        go.Frame(name="child", baseframe="base", group="selected-group", data=[go.Bar(y=[5, 1], marker={"color": CYAN})], traces=[0], layout={"title": {"text": "Inherited frame"}})]
    controls = {"mode": "immediate", "frame": {"duration": 0, "redraw": True}, "transition": {"duration": 0}}
    inherited.update_layout(margin={"t": 70}, updatemenus=[{"type": "buttons", "buttons": [{"label": "Group", "method": "animate", "args": ["selected-group", controls]}]}],
        sliders=[{"active": 0, "steps": [{"label": "Original", "method": "animate", "args": [["base"], controls]}, {"label": "Inherited", "method": "animate", "args": [["child"], controls]}]}])
    out["animation_inheritance"] = inherited
    unsupported = go.Figure(out["frame_bar"])
    unsupported.layout.updatemenus[0].buttons = [go.layout.updatemenu.Button(label="Unsupported title", method="animate", args=[["next"], {
        "frame": {"duration": 300, "redraw": False}, "transition": {"duration": 240}}])]
    out["unsupported_timed_title"] = unsupported
    return out
