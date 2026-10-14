"""Finite animation branches selected from the pinned renderer's schema.

Architecture: enumerated easing names and animated attributes -> genuine Figures
-> independent import bytes and saved native Run history -> paired drawn states.
Main callers: plotly_animation_fixtures and the isolated encoding probe.
Why this exists: renderer modules share easing dispatch, but nested styles,
implicit coordinates, point identity and retained frame replacement have their
own observable paths. These fixtures name those paths without multiplying every
possible user attribute combination.
"""
from __future__ import annotations

EASING_BASES = ("linear", "quad", "cubic", "sin", "exp", "circle", "elastic", "back", "bounce")
ALL_EASINGS = tuple(base + suffix for suffix in ("", "-in", "-out", "-in-out") for base in EASING_BASES)
TIMED_FAMILIES = ("scatter", "bar", "quiver", "indicator", "sunburst", "treemap", "icicle")


def branch_figures():
    """Save concrete missing coordinate, style, identity and frame boundaries.

    Main callers: genuine_figures and native_fixture. Fixed Cartesian ranges
    make SVG coordinates useful evidence. Each base/target remains a genuine
    Plotly input; browser expectations are independent native drawing results.
    """
    import plotly.graph_objects as go
    from plotly_gap_figures import MAGENTA, figures
    from plotly_gap_options_figures import CYAN

    layout = {"template": "none", "width": 680, "height": 430, "paper_bgcolor": "#171b20", "plot_bgcolor": "#171b20",
        "font": {"color": "#c5cbd3"}, "showlegend": False, "uirevision": "branches",
        "margin": {"l": 55, "r": 25, "t": 70, "b": 40}, "xaxis": {"range": [-1, 8]}, "yaxis": {"range": [-1, 10]}}

    def saved(name, before, after, *, redraw=False, frame_layout=None, traces=None):
        figure = go.Figure(data=before, layout=layout)
        figure.frames = [go.Frame(name="next", data=after, traces=traces, layout=frame_layout)]
        figure.update_layout(updatemenus=[{"type": "buttons", "buttons": [{"label": "Animate", "method": "animate",
            "args": [["next"], {"mode": "immediate", "frame": {"duration": 900, "redraw": redraw},
                "transition": {"duration": 900, "easing": "linear"}}]}]}])
        out["branch_" + name] = figure

    out = {}
    saved("scatter_marker_style",
        [go.Scatter(x=[2, 4], y=[2, 4], mode="markers", marker={"color": MAGENTA, "size": 18, "opacity": .4,
            "symbol": "arrow", "angle": 30, "standoff": 0, "line": {"color": MAGENTA, "width": 1}})],
        [go.Scatter(marker={"color": CYAN, "size": 36, "opacity": .9, "standoff": 12, "line": {"color": CYAN, "width": 5}})])
    saved("scatter_line_fill",
        [go.Scatter(x=[1, 3, 5], y=[2, 5, 2], mode="lines", fill="tozeroy", fillcolor=MAGENTA,
            line={"color": MAGENTA, "width": 2, "simplify": False})],
        [go.Scatter(fillcolor=CYAN, line={"color": CYAN, "width": 8, "simplify": False})])
    saved("scatter_implicit_x", [go.Scatter(x0=0, dx=1, y=[2, 4, 6], mode="markers", marker={"color": MAGENTA, "size": 18})],
        [go.Scatter(x0=2, dx=2)])
    saved("scatter_implicit_y", [go.Scatter(x=[1, 3, 5], y0=0, dy=1, mode="markers", marker={"color": MAGENTA, "size": 18})],
        [go.Scatter(y0=2, dy=2)])
    saved("scatter_ids", [go.Scatter(ids=["a", "b", "c"], x=[1, 3, 5], y=[2, 4, 6], mode="markers", marker={"color": MAGENTA, "size": 18})],
        [go.Scatter(ids=["c", "a", "b"], x=[2, 4, 6], y=[7, 3, 5])])
    saved("bar_marker_line", [go.Bar(x=[1, 3], y=[2, 4], marker={"color": MAGENTA, "line": {"color": CYAN, "width": 1}})],
        [go.Bar(marker={"line": {"width": 8}})])
    saved("bar_implicit_x", [go.Bar(x0=0, dx=1, y=[2, 4, 6], marker={"color": MAGENTA})], [go.Bar(x0=2, dx=2)])
    saved("bar_implicit_y", [go.Bar(x=[2, 4, 6], y0=0, dy=1, orientation="h", marker={"color": MAGENTA})], [go.Bar(y0=2, dy=2)])
    saved("quiver_xy_uv", [go.Quiver(x=[1, 3, 5], y=[2, 4, 2], u=[1, .5, -1], v=[.5, 1, .5], marker={"color": MAGENTA})],
        [go.Quiver(x=[2, 4, 6], y=[3, 5, 3], u=[.5, 1, -2], v=[1, .5, 1])])
    saved("quiver_implicit_xy", [go.Quiver(x0=0, dx=1, y0=0, dy=1, u=[1, .5, -1], v=[.5, 1, .5], marker={"color": MAGENTA})],
        [go.Quiver(x0=2, dx=2, y0=2, dy=2)])
    saved("quiver_explicit_xy", [go.Quiver(x=[0, 1, 2], y=[0, 1, 2], u=[1, .5, -1], v=[.5, 1, .5], marker={"color": MAGENTA})],
        [go.Quiver(x=[2, 4, 6], y=[2, 4, 6])])
    saved("quiver_template_null", [go.Quiver(x=None, y=None, x0=0, y0=0, u=[1, 1], v=[1, 1], marker={"color": MAGENTA})],
        [go.Quiver(u=[2, 2], v=[1, 1])])
    saved("quiver_template_frame_type", [go.Scatter(y=[0, 1], mode="markers", marker={"color": MAGENTA, "size": 18})],
        [go.Quiver(x0=0, y0=0, u=[1, 1], v=[1, 1], marker={"color": MAGENTA})], redraw=True)
    for name in ["branch_quiver_template_null", "branch_quiver_template_frame_type"]:
        out[name].layout.template = go.layout.Template(data={"quiver": [go.Quiver(x=[0, 1], y=[0, 1])]})
    # Preserve the actual wire distinction. Python's property setters omit null
    # values and choose scatter for an untyped frame fragment. The genuine
    # Figure serializer and both SDKs also accept these saved JSON fields, so
    # retain them here rather than accidentally testing a different payload.
    out["branch_quiver_template_null"]._data[0].update(x=None, y=None)
    inherited = go.Figure(out["branch_quiver_template_frame_type"])
    inherited.layout.template = "none"
    inherited.data[0].x = [0, 1]
    inherited.frames = [go.Frame(name="base", data=[go.Quiver(x=[0, 1], y=[0, 1], u=[1, 1], v=[1, 1])]),
        go.Frame(name="next", baseframe="base", data=[{"x": None, "y": None, "x0": 0, "y0": 0}])]
    inherited.frames[1]._props["data"] = [{"x": None, "y": None, "x0": 0, "y0": 0}]
    out["quiver_inherited_type_null"] = inherited
    saved("remapped_traces", [go.Scatter(uid="first", x=[1], y=[2], mode="markers", marker={"color": MAGENTA, "size": 20}),
        go.Scatter(uid="second", x=[5], y=[4], mode="markers", marker={"color": CYAN, "size": 20})],
        [go.Scatter(y=[8]), go.Scatter(y=[6])], traces=[1, 0])
    saved("layout_ranges_styles", [go.Scatter(x=[2, 4], y=[2, 4], mode="markers", marker={"color": MAGENTA, "size": 18})],
        [go.Scatter()], frame_layout={"xaxis": {"range": [0, 16]}, "yaxis": {"range": [0, 20]},
            "shapes": [{"type": "line", "x0": 1, "x1": 5, "y0": 5, "y1": 5, "line": {"color": CYAN, "width": 7}}],
            "selections": [{"x0": 1, "x1": 5, "y0": 1, "y1": 5, "line": {"color": CYAN, "width": 5}}]})
    out["branch_layout_ranges_styles"].update_layout(shapes=[{"type": "line", "x0": 1, "x1": 5, "y0": 5, "y1": 5, "line": {"color": MAGENTA, "width": 1}}],
        selections=[{"x0": 1, "x1": 5, "y0": 1, "y1": 5, "line": {"color": MAGENTA, "width": 1}}])
    out["native_layout_empty_update"] = go.Figure(out["branch_layout_ranges_styles"])
    out["branch_layout_ranges_styles"].frames[0].data = [go.Scatter(y=[3, 6])]
    ranges = go.Figure(out["branch_layout_ranges_styles"])
    ranges.frames[0].layout = {"xaxis": {"range": [0, 16]}, "yaxis": {"range": [0, 20]}}
    out["branch_layout_ranges"] = ranges
    styles = go.Figure(out["branch_layout_ranges_styles"])
    styles.frames[0].layout.xaxis = None
    styles.frames[0].layout.yaxis = None
    out["branch_layout_styles"] = styles
    out["native_equal_duration_ranges"] = go.Figure(ranges)
    for name in ["branch_layout_ranges_styles", "branch_layout_ranges", "branch_layout_styles"]:
        out[name].layout.updatemenus[0].buttons[0].args[1]["frame"]["duration"] = 1100
        out[name].layout.uirevision = None
    polar = go.Figure(figures()["scatterpolar"])
    polar.update_layout(polar={"radialaxis": {"range": [0, 4]}}, updatemenus=[{"type": "buttons", "buttons": [{"label": "Animate", "method": "animate",
        "args": [["next"], {"mode": "immediate", "frame": {"duration": 1100, "redraw": False}, "transition": {"duration": 900, "easing": "linear"}}]}]}])
    polar.frames = [go.Frame(name="next", data=[go.Scatterpolar(r=[1, 3, 2])], layout={"polar": {"radialaxis": {"range": [0, 8]}}})]
    out["branch_layout_polar"] = polar
    carpet = go.Figure(figures()["carpet"])
    carpet.data[0].ids = ["a", "b", "c", "d"]
    carpet.frames = [go.Frame(name="next", data=[go.Carpet(ids=["d", "c", "b", "a"])])]
    carpet.update_layout(updatemenus=[{"type": "buttons", "buttons": [{"label": "Animate", "method": "animate", "args": [["next"],
        {"mode": "immediate", "frame": {"duration": 900, "redraw": False}, "transition": {"duration": 900, "easing": "linear"}}]}]}])
    out["branch_carpet_ids"] = carpet
    retained = go.Figure(out["branch_remapped_traces"])
    out["retained_frames_before"] = retained
    changed = go.Figure(retained)
    changed.data = tuple(reversed(changed.data))
    changed.data[0].y = [1]
    changed.data[1].y = [3]
    changed.frames = [go.Frame(name="replacement", traces=[0, 1], data=[go.Scatter(y=[7]), go.Scatter(y=[9])])]
    changed.layout.updatemenus[0].buttons[0].args = [["replacement"], {"mode": "immediate", "frame": {"duration": 0, "redraw": False}, "transition": {"duration": 0}}]
    out["retained_frames_after"] = changed
    for name, baseframe in [("missing_inheritance", "absent"), ("cyclic_inheritance", "next")]:
        broken = go.Figure(out["branch_scatter_implicit_x"])
        broken.frames[0].baseframe = baseframe
        out[name] = broken
    return out
