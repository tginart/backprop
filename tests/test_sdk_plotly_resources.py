"""Finite Plotly resources survive logging, retries and immutable artifacts.

Architecture: a local HTTP fixture records the explicit requested paths and
headers -> actual SDK rows stage bytes -> MemorySink/public artifact readback
verifies the same bytes after the HTTP source changes. No optional Plotly package
is needed to exercise its JSON contract or any external service is contacted.
"""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.plotly_resources import capture, resource_fields
from backprop.transport import MemorySink, read_journal, sync

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGOMW32LgYGBiQEMABZSAefaaOKmAAAAAElFTkSuQmCC")


class PlotlyResourceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = MemorySink()
        self.calls, self.runs = [], []
        self.responses = {"/image.png": ("image/png", PNG),
                          "/region.json": ("application/json", b'{"type":"FeatureCollection","features":[]}')}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.calls.append((self.path, dict(self.headers)))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/image.png")
                    self.end_headers()
                    return
                value = owner.responses.get(self.path)
                self.send_response(200 if value else 404)
                self.send_header("Content-Type", value[0] if value else "text/plain")
                self.end_headers()
                self.wfile.write(value[1] if value else b"not found")
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.environment = patch.dict(os.environ, {"FOUNDRY_TOKEN": "never-forward-this-token"}, clear=True)
        self.environment.start()

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def new(self, offline=False):
        run = backprop.Run(project="plotly-resources", id="probe", dir=self.root,
            mode="offline" if offline else "online", _sink=None if offline else self.store,
            _deployment={}, settings={"console": "off", "silent": True, "disable_git": True,
                                      "flush_interval": 3600})
        self.runs.append(run)
        return run

    def test_known_paths_include_templates_frames_styles_but_not_custom_data(self):
        url = self.url
        spec = {"data": [{"type": "image", "source": url + "/image.png", "customdata": {"source": url + "/never"}},
                         {"type": "choroplethmap", "geojson": url + "/region.json"}],
                "layout": {"images": [{"source": url + "/image.png"}],
                           "map2": {"style": {"version": 8, "sources": {
                               "region": {"type": "geojson", "data": url + "/region.json"},
                               "tiles": {"type": "raster", "tiles": [url + "/{z}/{x}/{y}"]}}},
                               "layers": [{"sourcetype": "image", "source": url + "/image.png"}]},
                           "template": {"data": {"image": [{"source": url + "/image.png"}]},
                                        "layout": {"images": [{"source": url + "/image.png"}]}}},
                "frames": [{"data": [{"type": "image", "source": url + "/image.png"}],
                            "layout": {"mapbox": {"layers": [{"source": url + "/region.json"}]}}}]}
        paths = [item[0] for item in resource_fields(spec)]
        self.assertEqual(len(paths), 9)
        value = backprop.Plotly(spec)
        self.assertEqual(len(capture(value)), 9)
        self.assertEqual([item[0] for item in self.calls], ["/image.png", "/region.json"])
        self.assertEqual(value.spec, spec)
        self.assertTrue(all("Authorization" not in headers and "Cookie" not in headers for _, headers in self.calls))

    def test_style_relative_finite_inputs_are_captured_without_dynamic_requests(self):
        style = {"version": 8, "sources": {"area": {"type": "geojson", "data": "region.json"},
                 "image": {"type": "image", "url": "image.png"},
                 "tiles": {"type": "vector", "url": "tiles.json"}},
                 "glyphs": "fonts/{fontstack}/{range}.pbf", "sprite": "sprites/map"}
        self.responses["/style.json"] = ("application/json", json.dumps(style).encode())
        value = backprop.Plotly({"layout": {"map": {"style": self.url + "/style.json"}}})
        resource = capture(value)[0]
        self.assertEqual(resource["data"], self.responses["/style.json"][1])
        self.assertEqual([item["path"] for item in resource["resources"]], [["sources", "area", "data"], ["sources", "image", "url"]])
        self.assertEqual(resource["base_url"], self.url + "/style.json")
        self.assertEqual([call[0] for call in self.calls], ["/style.json", "/region.json", "/image.png"])

    def test_offline_then_sync_freezes_files_and_relog_never_refetches(self):
        run = self.new(offline=True)
        value = backprop.Plotly({"data": [{"type": "image", "source": self.url + "/image.png"}]})
        run.log({"plot": value})
        self.responses["/image.png"] = ("image/png", b"changed")
        run.log({"plot": value})
        run.finish()
        sync(run.sync_dir, sink=self.store)
        events = [event for event in self.store.events if event["type"] == "history"]
        self.assertEqual(len(events), 2)
        refs = [event["payload"]["metrics"]["plot"]["resources"][0]["file"] for event in events]
        self.assertEqual(refs[0], refs[1])
        self.assertEqual(self.store.download_file(refs[0]), PNG)
        self.assertEqual(len(self.calls), 1)

    def test_artifact_and_table_roundtrip_keep_resources_owner_local(self):
        value = backprop.Plotly({"data": [{"type": "image", "source": self.url + "/image.png"}]})
        artifact = backprop.Artifact("plots", "dataset")
        entry = artifact.add(backprop.Table(columns=["plot"], data=[[value]]), "figures")
        payload = json.loads(entry._read_verified_bytes())["data"][0][0]
        self.assertTrue(payload["resources"][0]["file"]["artifact_path"].startswith("media/"))
        run = self.new()
        run.log_artifact(artifact)
        run.log({"table": backprop.Table(columns=["plot"], data=[[value]])})
        run.finish()
        self.responses.clear()
        api = backprop.Api._from_store(self.store, project=run.project)
        remote = api.artifact(artifact.qualified_name)
        restored = remote.get("figures").data[0][0]
        second = backprop.Artifact("copied", "dataset")
        copied = second.add(backprop.Table(columns=["plot"], data=[[restored]]), "figures")
        ref = json.loads(copied._read_verified_bytes())["data"][0][0]["resources"][0]["file"]
        self.assertEqual(second.get_entry(ref["path"])._read_verified_bytes(), PNG)
        table_artifact = [a for a in api.run(run.id).logged_artifacts() if a.type == "run_table"][0]
        self.assertEqual(capture(table_artifact.get("table").data[0][0])[0]["data"], PNG)
        self.assertEqual(len(self.calls), 1)

    def test_rejected_row_and_failed_capture_publish_no_history_or_partial_cache(self):
        run = self.new()
        value = backprop.Plotly({"layout": {"images": [{"source": self.url + "/image.png"}, {"source": self.url + "/missing?secret=private"}]}})
        with self.assertRaisesRegex(ValueError, "HTTPError") as caught:
            run.log({"plot": value})
        self.assertNotIn("private", str(caught.exception))
        self.assertFalse(hasattr(value, "_resource_snapshots"))
        valid = backprop.Plotly({"data": [{"type": "image", "source": self.url + "/image.png"}]})
        with self.assertRaises(TypeError):
            run.log({"plot": valid, "invalid": object()})
        self.assertFalse(any(event["type"] == "history" for event in read_journal(run.sync_dir)))

    def test_limits_and_invalid_json_are_explicit(self):
        value = backprop.Plotly({"data": [{"type": "image", "source": self.url + "/image.png"}]})
        with patch("backprop.plotly_resources.MAX_RESOURCE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "per-file limit"):
                capture(value)
        self.responses["/bad.json"] = ("application/json", b'[]')
        with self.assertRaisesRegex(ValueError, "must contain an object"):
            capture(backprop.Plotly({"data": [{"type": "choropleth", "geojson": self.url + "/bad.json"}]}))

    def test_embedded_pixels_data_urls_and_ordinary_figures_do_not_read_network(self):
        spec = {"data": [{"type": "image", "z": [[[0, 0, 0]]]},
                         {"type": "image", "source": "data:image/png;base64,aGVsbG8="},
                         {"type": "scatter", "x": [1], "y": [2], "source": self.url + "/never"}]}
        self.assertEqual(capture(backprop.Plotly(spec)), [])
        self.assertEqual(self.calls, [])

    def test_frame_trace_inherits_explicit_parent_type_and_remapping(self):
        value = backprop.Plotly({"data": [{"type": "scatter"}, {"type": "image", "z": [[[0, 0, 0]]]}],
            "frames": [{"traces": [1], "data": [{"source": self.url + "/image.png"}]}]})
        resources = capture(value)
        self.assertEqual(resources[0]["path"], ["frames", 0, "data", 0, "source"])
        self.assertEqual(resources[0]["data"], PNG)

    def test_capture_deadline_applies_even_when_a_response_keeps_making_progress(self):
        value = backprop.Plotly({"data": [{"type": "image", "source": self.url + "/image.png"}]})
        with patch("backprop.plotly_resources.time.monotonic", side_effect=[0, 16]):
            with self.assertRaisesRegex(ValueError, "timed out"):
                capture(value)
        self.assertFalse(hasattr(value, "_resource_snapshots"))

    def test_inline_control_frames_capture_exact_paths_and_parent_types_once(self):
        image = self.url + "/image.png"
        inline = {"traces": [1], "data": [{"source": image}], "layout": {"images": [{"source": image}]}}
        spec = {"data": [{"type": "scatter"}, {"type": "image", "z": [[[0, 0, 0]]]}],
            "layout": {"updatemenus": [{"buttons": [{"method": "animate", "args": [[inline], {}], "args2": [inline, {}]}]}],
                "sliders": [{"steps": [{"method": "animate", "args": [[inline], {}]}]}]}}
        value = backprop.Plotly(spec)
        before = json.dumps(value.spec)
        resources = capture(value)
        roots = [["layout", "updatemenus", 0, "buttons", 0, "args", 0, 0],
                 ["layout", "updatemenus", 0, "buttons", 0, "args2", 0],
                 ["layout", "sliders", 0, "steps", 0, "args", 0, 0]]
        self.assertEqual([item["path"] for item in resources],
            [root + field for root in roots for field in (["data", 0, "source"], ["layout", "images", 0, "source"])])
        self.assertTrue(all(item["data"] == PNG for item in resources))
        run = self.new()
        run.log({"plot": value})
        run.finish()
        self.assertEqual(json.dumps(value.spec), before)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all("Authorization" not in headers and "Cookie" not in headers for _, headers in self.calls))


if __name__ == "__main__":
    unittest.main()
