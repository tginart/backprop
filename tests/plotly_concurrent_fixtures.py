"""Genuine SDK history for finite animation/update ownership regressions.

Architecture: existing genuine layout Figures -> distinct saved controls/frames
and independent static targets -> SDK Run/MemorySink history and captured PNG.
Main callers: plotly_concurrent_browser_qa. Why this exists: object mutations in
JavaScript cannot prove that real saved SDK descriptors survive the same race.
"""
from __future__ import annotations
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGOMW32LgYGBiQEMABZSAefaaOKmAAAAAElFTkSuQmCC")


def native_fixture():
    """Log every animated value and independently constructed final target.

    Main caller: the focused browser runner's Python child. A loopback source
    supplies one actually captured resource. The browser later exercises failed
    and delayed Foundry readers against those unchanged SDK resource bytes.
    """
    import plotly.graph_objects as go
    import backprop as w
    from plotly_layout_fixtures import figures
    sources, goldens, _, _ = figures()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(PNG)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        values = {}
        for index, (label, source) in enumerate([("A", "ranges_styles"), ("B", "template_selection"), ("C", "array_merge"), ("D", "ranges_styles"), ("plain", "plain_working")], 1):
            fig = go.Figure(sources[source])
            base, target = [go.Figure(goldens[source][key]) for key in ["base", "next"]]
            for value in [fig, base]:
                value.data[0].y = [2 * index, 4 * index]
                value.layout.yaxis.range = [-1, 10 * index]
            target.data[0].y = [3 * index, 6 * index]
            target.layout.yaxis.range = [0, 20 * index]
            base_layout, next_layout = [frame.layout.to_plotly_json() for frame in fig.frames]
            base_layout["yaxis"] = {"range": [-1, 10 * index]}
            next_layout["yaxis"] = {"range": [0, 20 * index]}
            fig.frames = [go.Frame(name="base_" + label, data=base.data, layout=base_layout), go.Frame(name="next_" + label, data=target.data, layout=next_layout)]
            options = {"mode": "immediate", "frame": {"duration": 1000, "redraw": False}, "transition": {"duration": 600, "easing": "linear"}}
            controls = [{"label": "Next " + label, "method": "animate", "args": [["next_" + label], copy.deepcopy(options)]}, {"label": "Back " + label, "method": "animate", "args": [["base_" + label], copy.deepcopy(options)]}, {"label": "Queue " + label, "method": "animate", "args": [["next_" + label, "base_" + label, "next_" + label], copy.deepcopy(options)]}]
            for value in [fig, base, target]:
                value.update_layout(title={"text": "Concurrent " + label}, updatemenus=[{"type": "buttons", "buttons": controls}], sliders=[], transition={"duration": 400, "easing": "linear", "ordering": "layout first"}, overwrite=True)
            if label == "D":
                fig.add_layout_image(source=f"http://127.0.0.1:{server.server_port}/captured.png", x=0, y=1, xref="paper", yref="paper", sizex=.15, sizey=.15)
                for value in [base, target]:
                    value.add_layout_image(source="data:image/png;base64," + base64.b64encode(PNG).decode(), x=0, y=1, xref="paper", yref="paper", sizex=.15, sizey=.15)
            values[label], values[label + "_base"], values[label + "_next"] = fig, base, target
        with tempfile.TemporaryDirectory() as directory:
            sink = w.MemorySink()
            packets, bytes_sha = {}, {}
            for name, value in values.items():
                packet = w.Plotly(value)
                packets[name] = packet
                bytes_sha[name] = hashlib.sha256(Path(packet._path).read_bytes()).hexdigest()
            with w.Run(id="plotly-concurrent", entity="tests", project="plotly", dir=directory, _sink=sink, _deployment={}, settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True}) as run:
                run.log(packets)
            row = next(w.Api._from_store(sink, entity="tests", project="plotly").run(run.id).scan_history())
            descriptors = {key: value for key, value in row.items() if not key.startswith("_")}
            resources = {item["file"]["path"]: base64.b64encode(sink.files[item["file"]["path"]]).decode() for item in descriptors["D"]["resources"]}
            return {"values": descriptors, "figure_bytes_sha256": bytes_sha, "captured_resource_bytes": resources}
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    print(json.dumps(native_fixture(), sort_keys=True))
