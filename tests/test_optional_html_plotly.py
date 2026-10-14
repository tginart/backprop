"""Validate HTML/Plotly public contracts and durable native logging.

Architecture: isolated pinned comparisons -> explicit Foundry differences ->
actual journals, retry/restart and artifact reads. HTML does not require optional
plotting libraries; Plotly Figure conversion is also tested by its runtime suite.
Main callers: full SDK and optional CI suites. Tests install no dependencies.
"""
import copy
import concurrent.futures
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop as w
from backprop.apis.public.replay import reduce_events
from backprop.transport import BackpropError

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True, "flush_interval": 600}


class HtmlPlotlyContracts(unittest.TestCase):
    def test_file_contracts_match_pinned_with_explicit_plotly_decoder_repair(self):
        try: reference = importlib.metadata.distribution("wandb")
        except importlib.metadata.PackageNotFoundError: self.skipTest("Pinned W&B wheel unavailable")
        self.assertEqual(reference.version, "0.30.0")
        observations = []
        for mode in ["reference", "native"]:
            process = subprocess.run([sys.executable, "-B", str(ROOT / "tests/html_plotly_reference_probe.py"), mode],
                capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir(),
                env={**os.environ, "WANDB_DATA_DIR": str(Path(tempfile.gettempdir()) / "backprop-reference-data"), "PYTHONPATH": str(reference.locate_file("")) if mode == "reference" else str(ROOT / "sdk")})
            self.assertEqual(process.returncode, 0, process.stderr)
            observations.append(json.loads(process.stdout))
            if os.environ.get("CI"):
                target = ROOT / "test-results"; target.mkdir(exist_ok=True)
                (target / f"html-plotly-{mode}.json").write_text(process.stdout)
        hashes = observations[0].pop("source_hashes")
        for name, digest in hashes.items():
            import hashlib
            path = ROOT / ".reference/wandb/source/wandb/sdk/data_types" / (name.lower() + ".py")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        for alias in ["wandb.Plotly", "wandb.data_types.Plotly", "wandb.sdk.data_types.plotly.Plotly"]:
            self.assertEqual(observations[0]["aliases"][alias].pop("decode"), {"error": "ValueError"})
            self.assertEqual(observations[1]["aliases"][alias].pop("decode"), {"value": True})
        # The literal path input is necessarily different in isolated processes.
        for observed in observations:
            observed["html_inputs"][7]["value"] = "<isolated-source-path>"
        self.assertEqual(observations[0], observations[1])

    def test_html_plotly_public_files_native_history_artifacts_retry_and_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(w.Artifact, "_default_root",
                lambda artifact, **kwargs: str(Path(directory, "downloads", artifact._name))):
            sink = w.MemorySink()
            run = w.Run(id="html-plotly", entity="e", project="media", dir=directory, _sink=sink, _deployment={}, settings=SETTINGS)
            self.addCleanup(run.finish)
            html = w.Html("<h1>saved</h1>", inject=False)
            plot = w.Plotly({"data": [{"type": "scatter3d", "x": [1, 2], "y": [2, 3], "z": [4, 5]}]})
            expected = copy.deepcopy(plot.spec)
            for value, key in [(html, "html"), (plot, "plot")]:
                value.bind_to_run(run, key, 1, id_="chosen")
                descriptor = value.to_json(run)
                self.assertTrue(Path(run.dir, descriptor["path"]).is_file())
                artifact = w.Artifact(key + "-files", "dataset")
                public = value.to_json(artifact)
                restored = type(value).from_json(public, artifact)
                self.assertTrue(restored.file_is_set())
                self.assertFalse(restored.is_bound())
                self.assertEqual(restored.to_json(None), {"_type": value._log_type})
                if key == "html": self.assertEqual(restored.html, "<h1>saved</h1>")
                else: self.assertEqual(restored.spec, expected)
            artifact = w.Artifact("rich-values", "dataset")
            artifact.add(html, "html")
            artifact.add(w.Table(columns=["plot"], data=[[plot]]), "plots")
            run.log_artifact(artifact, aliases=["best"])
            run.log({"html": html, "plot": plot})
            original = sink.publish
            def lose_ack(events):
                original(events)
                raise OSError("lost media event acknowledgement")
            with patch.object(sink, "publish", side_effect=lose_ack):
                with self.assertRaises(BackpropError): run.flush()
            run.flush(); run.finish()
            before = reduce_events(sink.events)
            self.assertEqual(before, reduce_events(list(reversed(sink.events)) + sink.events))
            api = w.Api._from_store(sink, entity="e", project="media")
            row = list(api.run(run.id).scan_history())[0]
            self.assertEqual(sink.download_file(row["html"]), b"<h1>saved</h1>")
            self.assertEqual(row["plot"]["spec"], expected)
            saved = api.artifact("rich-values:best")
            restored_html = saved.get("html")
            self.assertEqual(restored_html.html, "<h1>saved</h1>")
            self.assertEqual(saved.get("plots").data[0][0].spec, expected)
            secondary = w.Artifact("copy-html", "dataset")
            descriptor = restored_html.to_json(secondary)
            self.assertEqual(secondary.get_entry(descriptor["path"])._read_verified_bytes(), b"<h1>saved</h1>")
            restarted = w.Run(id=run.id, entity="e", project="media", dir=directory, _sink=sink, _deployment={}, settings=SETTINGS, resume="must")
            self.addCleanup(restarted.finish)
            restarted.log({"html": restored_html, "plot": saved.get("plots").data[0][0]})
            restarted.finish()
            self.assertEqual(len(list(api.run(run.id).scan_history())), 2)

    def test_temporary_files_are_distinct_across_threads_and_html_bytes_remain_frozen(self):
        def create(index):
            return w.Html(f"<p>{index}</p>", inject=False), w.Plotly({"data": [{"x": [index]}]})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(create, range(24)))
        paths = [Path(value._path) for pair in values for value in pair]
        self.assertEqual(len(set(paths)), 48)
        for index, (html, plot) in enumerate(values):
            frozen = html._bytes()
            html.inject_head()
            self.assertNotEqual(html.html.encode(), frozen)
            self.assertEqual(html._bytes(), frozen)
            self.assertEqual(json.loads(Path(plot._path).read_text())["data"][0]["x"], [index])

    def test_plotly_decoder_reports_corrupt_missing_and_invalid_spec_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(w.Artifact, "_default_root",
                lambda artifact, **kwargs: str(Path(directory, "downloads", artifact._name))):
            artifact = w.Artifact("invalid-spec", "dataset")
            for name, data in [("corrupt", b"{"), ("empty", b"null")]:
                path = Path(directory, name + ".json"); path.write_bytes(data)
                artifact.add_file(path, name=path.name)
                with self.assertRaises((ValueError, TypeError)):
                    w.Plotly.from_json({"path": path.name}, artifact)
            with self.assertRaises(KeyError): w.Plotly.from_json({}, artifact)
            with self.assertRaises(Exception): w.Plotly.from_json({"path": "missing"}, artifact)


if __name__ == "__main__": unittest.main()
