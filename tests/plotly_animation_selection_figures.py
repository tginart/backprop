"""Finite frame selection and retained-update inputs for genuine SDK encoding.

Architecture: explicit saved controls -> genuine Figures -> isolated alias bytes
and real Run history -> paired viewer/native drawing observations.
Main callers: plotly_animation_fixtures.genuine_figures and its history publisher.
Why this exists: reverse/fromcurrent filtering acts on selection order, whereas
layout transition ordering acts on the distinct Plotly.react lifecycle.
"""
from __future__ import annotations

SELECTED_EASINGS = ("cubic-in-out", "sin-out", "back-out", "elastic-out", "bounce-out")


def selection_figures():
    """Save independent selection, easing and axis/data ordering requests.

    Main caller: genuine_figures. Fixed axes and one visible point give browser
    measurements a direct relation to the saved numerical values. Native frame
    endpoint behavior is asserted by the browser rather than rewritten here.
    """
    import plotly.graph_objects as go
    from plotly_gap_figures import MAGENTA

    def scatter(y=1, *, height=430, top=70):
        return go.Figure(data=[go.Scatter(x=[1], y=[y], mode="markers", marker={"color": MAGENTA, "size": 22})],
            layout={"template": "none", "width": 680, "height": height, "paper_bgcolor": "#171b20", "plot_bgcolor": "#171b20",
                "font": {"color": "#c5cbd3"}, "showlegend": False, "uirevision": "selection", "margin": {"l": 55, "r": 25, "t": top, "b": 40},
                "xaxis": {"range": [0, 2]}, "yaxis": {"range": [0, 10]}})

    def control(label, selection, *, reverse=False, resume=False, duration=90):
        return {"label": label, "method": "animate", "args": [selection, {"mode": "immediate", "direction": "reverse" if reverse else "forward",
            "fromcurrent": resume, "frame": {"duration": duration, "redraw": False}, "transition": {"duration": 0, "easing": "linear"}}]}

    figure = scatter(height=520, top=165)
    figure.frames = [go.Frame(name=name, group="sequence" if name != "d" else "outside", traces=[0], data=[go.Scatter(y=[value])])
        for name, value in [("a", 2), ("b", 4), ("c", 6), ("d", 8)]]
    inline = {"traces": [0], "data": [{"type": "scatter", "y": [3]}]}
    groups = [[control("Seed " + name, [name], duration=0) for name in "abcd"],
        [control("Forward", "sequence"), control("Reverse", "sequence", reverse=True), control("Resume", "sequence", resume=True), control("Reverse resume", "sequence", reverse=True, resume=True)],
        [control("All resume", None, resume=True), control("Explicit resume", ["c", "a", "b"], resume=True), control("Inline resume", [inline, "a", "b", "c"], resume=True)],
        [control("Inline reverse resume", [inline, "a", "b", "c"], reverse=True, resume=True), control("Array reverse", "sequence", reverse=True, resume=True)]]
    groups[-1][-1]["args"][1]["frame"] = [{"duration": 110, "redraw": False}, {"duration": 160, "redraw": False}]
    groups[-1][-1]["args"][1]["transition"] = [{"duration": 0, "easing": "linear"}, {"duration": 0, "easing": "quad-in"}]
    figure.update_layout(updatemenus=[{"type": "buttons", "direction": "right", "x": 0, "xanchor": "left", "y": 1.46 - index * .15,
        "yanchor": "top", "buttons": buttons} for index, buttons in enumerate(groups)])
    out = {"selection_animation": figure}
    numeric = scatter(height=470, top=105)
    numeric.frames = [go.Frame(name=str(index), group="7", traces=[0], data=[go.Scatter(y=[value])]) for index, value in [(1, 3), (2, 5)]]
    default = {"label": "Defaults", "method": "animate", "args": [["1"]]}
    empty = {"label": "Empty arrays", "method": "animate", "args": [["2"], {"frame": [], "transition": []}]}
    numeric.update_layout(updatemenus=[{"type": "buttons", "direction": "right", "x": 0, "xanchor": "left", "y": 1.3,
        "buttons": [control("Numeric group", 7), control("Numeric reverse", 7, reverse=True), control("Numeric names", [2, 1]), default, empty]}])
    out["numeric_selection_animation"] = numeric
    for easing in SELECTED_EASINGS:
        eased = scatter(y=2)
        eased.frames = [go.Frame(name="next", traces=[0], data=[go.Scatter(y=[8])])]
        animate = control("Animate", ["next"], duration=900)
        animate["args"][1]["transition"] = {"duration": 900, "easing": easing}
        eased.update_layout(updatemenus=[{"type": "buttons", "buttons": [animate]}])
        out["selected_easing_" + easing] = eased
    for ordering in ("layout first", "traces first"):
        stem = "react_" + ordering.replace(" ", "_")
        before = scatter(y=2)
        before.update_layout(transition={"duration": 900, "easing": "linear", "ordering": ordering})
        after = go.Figure(before)
        after.data[0].y = [10]
        after.update_layout(yaxis={"range": [0, 20]})
        out[stem + "_before"] = before
        out[stem + "_after"] = after
    return out
