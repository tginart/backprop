"""Real SDK layout-frame inputs, static golden figures and retained updates.

Architecture: genuine Figures -> independent alias encoding -> MemorySink Run
history -> native controls and independently rendered static target geometry.
Main callers: plotly_layout_browser_qa and plotly_layout_probe_encoding.
Why this exists: the pinned native animation defect is a negative source oracle;
ordinary newPlot of the requested state independently proves the repaired draw.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import tempfile


def figures():
    """Enumerate selection inheritance and finite control/lifecycle branches.

    Main callers: isolated encoding probe and native_fixture. Golden states are
    separately constructed Figures, not output from the viewer adapter.
    """
    import plotly.graph_objects as go
    from plotly_animation_branch_figures import branch_figures
    sources = branch_figures()
    out, golden = {}, {}
    for suffix in ["ranges_styles", "ranges", "styles"]:
        fig = go.Figure(sources["branch_layout_" + suffix])
        initial = {key: copy.deepcopy(fig.layout.to_plotly_json()[key]) for key in ["xaxis", "yaxis", "shapes", "selections"]}
        base = go.Frame(name="base", data=[trace.to_plotly_json() for trace in fig.data], layout=initial)
        next_frame = go.Frame(fig.frames[0])
        next_frame.name = "next"
        fig.frames = [base, next_frame]
        target = go.Figure(data=[go.Scatter(x=[2, 4], y=[3, 6], mode="markers", marker={"color": "#e83fe8", "size": 18})], layout=fig.layout)
        target.update_layout(next_frame.layout.to_plotly_json())
        golden[suffix] = {"base": go.Figure(data=fig.data, layout=fig.layout), "next": target}
        out[suffix] = fig
    for name in ["data_only", "template_selection", "null_selection", "array_merge", "baseframe", "inline", "plain_working"]:
        out[name] = go.Figure(out["ranges_styles"])
        golden[name] = {key: go.Figure(value) for key, value in golden["ranges_styles"].items()}
    out["data_only"].frames[1].layout = None
    golden["data_only"]["next"] = go.Figure(golden["data_only"]["base"])
    golden["data_only"]["next"].data[0].y = [3, 6]
    for fig in [out["template_selection"], *golden["template_selection"].values()]:
        saved = fig.layout.selections[0].to_plotly_json()
        saved["name"] = "saved-selection"
        fig.layout.template = go.layout.Template(layout={"selections": [saved]})
        fig.layout.selections = ()
    for frame in out["template_selection"].frames:
        frame.layout.selections[0].templateitemname = "saved-selection"
    # A genuine Figure preserves an explicit null on its internal JSON input.
    # The SDK serializes this accepted distinction; property setters omit null.
    out["null_selection"].frames[1]._props["layout"]["selections"] = None
    golden["null_selection"]["next"].layout.selections = ()
    second = {"x0": 5, "x1": 7, "y0": 2, "y1": 4, "line": {"color": "#e83fe8", "width": 2}}
    out["array_merge"].layout.selections = [out["array_merge"].layout.selections[0], second]
    out["array_merge"].frames[0].layout.selections = out["array_merge"].layout.selections
    out["array_merge"].frames[1].layout.selections = [{"x0": 2, "line": {"width": 5}}]
    golden["array_merge"]["base"].layout.selections = out["array_merge"].layout.selections
    golden["array_merge"]["next"].layout.selections = [{"x0": 2, "x1": 5, "y0": 1, "y1": 5, "line": {"color": "#e83fe8", "width": 5}}, second]
    inherited = go.Frame(name="inherited", data=out["baseframe"].frames[1].data, layout=out["baseframe"].frames[1].layout)
    out["baseframe"].frames = [out["baseframe"].frames[0], go.Frame(name="next", baseframe="inherited", layout={"selections": [{"x0": 2}]}), inherited]
    golden["baseframe"]["next"].layout.selections[0].x0 = 2
    for fig in [out["plain_working"], *golden["plain_working"].values()]:
        fig.layout.selections = ()
    for frame in out["plain_working"].frames:
        frame.layout.selections = ()
    for name, fig in out.items():
        options = {"mode": "immediate", "frame": {"duration": 360, "redraw": False}, "transition": {"duration": 180, "easing": "linear"}}
        select_next = [fig.frames[1].to_plotly_json()] if name == "inline" else ["next"]
        fig.update_layout(updatemenus=[{"type": "buttons", "buttons": [
            {"label": "Next", "method": "animate", "args": [select_next, copy.deepcopy(options)]},
            {"label": "Back", "method": "animate", "args": [["base"], copy.deepcopy(options)]},
            {"label": "Sequence", "method": "animate", "args": [["next", "base", "next"], copy.deepcopy(options)]},
            {"label": "Zero", "method": "animate", "args": [["next", "base", "next"], {"mode": "immediate", "frame": {"duration": 0, "redraw": False}, "transition": {"duration": 0}}]},
        ]}], sliders=[{"steps": [{"label": label, "method": "animate", "args": [[frame], copy.deepcopy(options)]} for label, frame in [("Base", "base"), ("Next", "next")]]}], overwrite=True)
        for value in golden[name].values():
            value.update_layout(updatemenus=fig.layout.updatemenus, sliders=fig.layout.sliders, overwrite=True)
    changed = go.Figure(out["ranges_styles"])
    changed.frames[1].data[0].y = [4, 8]
    changed.frames[1].layout.xaxis.range = [0, 20]
    changed.frames[1].layout.yaxis.range = [0, 24]
    changed.frames[1].layout.selections[0].x0 = 3
    changed_golden = go.Figure(golden["ranges_styles"]["next"])
    changed_golden.data[0].y = [4, 8]
    changed_golden.update_layout(xaxis={"range": [0, 20]}, yaxis={"range": [0, 24]})
    changed_golden.layout.selections[0].x0 = 3
    return out, golden, changed, changed_golden


def encoded_figures():
    """Publish every independent animated/static input to the alias probe.

    Main caller: plotly_layout_probe_encoding. Encoding remains separate from
    actual rendering, including the source renderer's unchanged failing frames.
    """
    animated, golden, changed, changed_golden = figures()
    return {**animated, **{name + "_" + state: value for name, states in golden.items() for state, value in states.items()}, "retained_changed": changed, "retained_golden": changed_golden}


def native_fixture():
    """Log real public descriptors and return their actual replayed history.

    Main caller: browser driver. A separate MemorySink event supplies each
    retained update; no fabricated browser spec or hosted service is involved.
    """
    import backprop as w
    animated, golden, changed, changed_golden = figures()
    with tempfile.TemporaryDirectory() as directory:
        sink = w.MemorySink()
        with w.Run(id="plotly-layout", entity="tests", project="plotly", dir=directory, _sink=sink, _deployment={}, settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True}) as run:
            run.log({name: w.Plotly(value) for name, value in encoded_figures().items()})
            for value in [animated["ranges_styles"], changed, animated["ranges_styles"], changed]:
                run.log({"retained": w.Plotly(value)})
        rows = list(w.Api._from_store(sink, entity="tests", project="plotly").run(run.id).scan_history())
        return {"values": {key: value for key, value in rows[0].items() if not key.startswith("_")}, "names": list(animated), "retained": [row["retained"] for row in rows[1:]]}


if __name__ == "__main__":
    print(json.dumps(native_fixture(), sort_keys=True))
