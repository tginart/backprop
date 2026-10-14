"""Produce genuine SDK HTML/Plotly events for Python/browser replay agreement.

Architecture: file binding -> journal publication -> public Python history and
browser events with identical immutable bytes. Main caller: JS replay tests.
"""
import base64
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop as w
from backprop.apis.public.replay import reduce_events


def fixture():
    with tempfile.TemporaryDirectory() as directory:
        sink = w.MemorySink()
        with w.Run(id="html-plotly", entity="e", project="media", dir=directory, _sink=sink, _deployment={},
                   settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True}) as run:
            html = w.Html("<h1>saved</h1>", inject=False)
            plot = w.Plotly({"data": [{"type": "scatter3d", "x": [1, 2], "y": [2, 3], "z": [4, 5]}]})
            html.bind_to_run(run, "html", 0, id_="html")
            plot.bind_to_run(run, "plot", 0, id_="plot")
            run.log({"html": html, "plot": plot})
        api = w.Api._from_store(sink, entity="e", project="media")
        rows = list(api.run(run.id).scan_history())
        history = [{k: v for k, v in row.items() if k not in {"_timestamp", "_runtime"}} for row in rows]
        return {"events": sink.events, "expected": reduce_events(sink.events), "history": history,
                "html": base64.b64encode(sink.download_file(rows[0]["html"])).decode()}


if __name__ == "__main__": print(json.dumps(fixture()))
