"""TensorBoard parity gaps using real writers and isolated pinned W&B processes.

Architecture: signature/alias probes run independently -> actual optional event
files exercise plugin conversion, writer interception, namespaces and restarts
-> normal Backprop public readback validates durable histories and media files.
Main callers: existing test_optional_*.py CI discovery. Missing TensorBoard,
TensorFlow and TensorBoardX remain explicit skips, never simulated frameworks.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api
from backprop.integration import tensorboard as tb
from backprop.transport import MemorySink
from tensorboard_gap_fixtures import events, png, tensor_value, write

ROOT = Path(__file__).resolve().parents[1]
HAS_TB = importlib.util.find_spec("tensorboard") is not None
HAS_MEDIA = importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("numpy") is not None
SETTINGS = {"silent": True, "console": "off", "x_disable_stats": True, "flush_interval": 3600}


class TensorBoardPortableContracts(unittest.TestCase):
    def test_actual_offline_transaction_log_capture_uses_completed_bytes(self):
        """Verify real core completion and verbatim capture without TensorBoard.

        Main caller: optional CI discovery. The core keeps its log buffered
        independently of final-summary responses, so this regression exercises
        public service teardown rather than substituting summary observations.
        """
        output = ROOT / "test-results/tensorboard-gap-transaction-readback-reference-raw.json"
        output.parent.mkdir(exist_ok=True)
        env = {**os.environ, "PYTHONPATH": "", "WANDB_DATA_DIR": "/tmp/backprop-reference-data",
               "WANDB_CACHE_DIR": "/tmp/backprop-reference-cache",
               "BACKPROP_TENSORBOARD_PROBE_OUTPUT": str(output)}
        result = subprocess.run([sys.executable, str(ROOT / "tests/tensorboard_gap_runtime_probe.py"),
                                 "reference", "transaction-readback"], env=env,
                                capture_output=True, text=True, timeout=120)
        output.with_name("tensorboard-gap-transaction-readback-reference-process.json").write_text(
            json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                        "stderr": result.stderr}, indent=2) + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"parser_values": [123]})
        raw = json.loads(output.read_text())
        transaction_bytes = output.with_suffix(".wandb").read_bytes()
        self.assertEqual(raw["transaction_capture_boundary"], "wandb.teardown")
        self.assertEqual(raw["transaction_size"], len(transaction_bytes))
        self.assertEqual(raw["transaction_sha256"], hashlib.sha256(transaction_bytes).hexdigest())
        self.assertTrue(any("history" in record for record in raw["transaction_records"]))

    def test_actual_history_paths_keep_rich_objects_and_literal_slash_tags(self):
        result = subprocess.run([sys.executable, str(ROOT / "tests/tensorboard_gap_runtime_probe.py"),
                                 "reference", "history-path-readback"], env={**os.environ, "PYTHONPATH": ""},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"legacy_weights": {"bins": [0, 1, 2], "values": [2, 3],
                                                               "_type": "histogram"},
                                             "train/loss": .25,
                                             "train/frame": {"filenames": ["media/images/frame.png"]},
                                             "global_step": 8})

    def test_pinned_complete_signatures_each_alias_and_unpatch(self):
        observations = {}
        for mode in ("reference", "native"):
            env = {**os.environ, "WANDB_DATA_DIR": "/tmp/backprop-reference-data"}
            env["PYTHONPATH"] = str(ROOT / "sdk") if mode == "native" else ""
            result = subprocess.run([sys.executable, str(ROOT / "tests/tensorboard_gap_probe.py"), mode],
                                    env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            observations[mode] = json.loads(result.stdout)
        reference, native = observations["reference"], observations["native"]
        self.assertEqual(reference["signatures"], native["signatures"])
        self.assertEqual(reference["aliases"], native["aliases"])
        self.assertTrue(all(native["aliases"].values()))
        for name in ("empty_unpatch", "missing_tensorboard", "patch", "double_patch", "restored", "switches"):
            if name in reference:
                self.assertEqual(reference[name], native[name], name)
        self.assertEqual(reference["sync_service_boundary"], {"run_path": "e/p/tb-import"})
        # Actual W&B0.30.0 deleted wandb.sync; its selected method still imports
        # it. The intended return above is a source-backed service boundary.
        if "sync_direct" in reference:
            self.assertEqual(reference["sync_direct"]["error"], "ModuleNotFoundError")
        output = ROOT / "test-results" / "tensorboard-gap-signatures.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(observations, indent=2) + "\n")

    def test_source_keys_preserve_copies_distinguish_writers_and_occurrences(self):
        from backprop.integration.tensorboard.reader import record_identity
        key = record_identity("train", "events.out.tfevents.1.host.2", b"event")
        self.assertEqual(key, record_identity("train", "events.out.tfevents.1.host.2", b"event"))
        self.assertNotEqual(key, record_identity("train", "events.out.tfevents.1.other.2", b"event"))
        self.assertNotEqual(key, record_identity("validation", "events.out.tfevents.1.host.2", b"event"))
        self.assertNotEqual(key, record_identity("train", "events.out.tfevents.1.host.2", b"event", 1))

    def test_default_directory_root_is_frozen_after_inference(self):
        tb.unpatch()
        try:
            tb._discover("/tmp/tb-root/train")
            with self.assertWarnsRegex(RuntimeWarning, "several event"):
                tb._discover("/tmp/tb-root/validation")
            self.assertEqual(tb.source_options("/tmp/tb-root/train"), (str(Path("/tmp/tb-root").resolve()), True))
            self.assertEqual(tb.source_options("/tmp/tb-root/validation"), (str(Path("/tmp/tb-root").resolve()), True))
            with self.assertWarnsRegex(RuntimeWarning, "several event"):
                tb._discover("/tmp/unrelated-tb-root")
            with self.assertWarnsRegex(RuntimeWarning, "outside of inferred"):
                self.assertEqual(tb.source_options("/tmp/unrelated-tb-root"), (str(Path("/tmp/unrelated-tb-root").resolve()), True))
            self.assertEqual(tb.source_options("/tmp/tb-root/train"), (str(Path("/tmp/tb-root").resolve()), True))
        finally:
            tb.unpatch()

    def test_pinned_go_large_histogram_rebin(self):
        from backprop.integration.tensorboard.codec import _histogram
        histogram = _histogram([1.] * 1024, list(range(1025)))
        self.assertEqual(histogram.histogram, [2.] * 512)
        self.assertEqual(histogram.bins, list(range(0, 1025, 2)))
        with self.assertRaisesRegex(ValueError, "increasing"):
            _histogram([1, 2], [0, 0, 2])


@unittest.skipUnless(HAS_TB and HAS_MEDIA, "optional tensorboard/Pillow/NumPy are not installed")
class TensorBoardRealEvents(unittest.TestCase):
    def setUp(self):
        tb.unpatch()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BACKPROP_DIR": self.tmp.name})
        self.env.start()
        self.sink = MemorySink()
        self.runs = []

    def tearDown(self):
        for run in self.runs:
            run.finish()
        tb.unpatch()
        self.env.stop()
        self.tmp.cleanup()

    def run_for(self, identity="tb-run", **kwargs):
        run = backprop.Run(id=identity, entity="e", project="tb-gap", dir=self.tmp.name,
                           settings=SETTINGS, _sink=self.sink, **kwargs)
        self.runs.append(run)
        return run

    def rows(self, run):
        run.flush()
        return list(Api._from_store(self.sink, entity="e", project="tb-gap").run(run.id).scan_history())

    def test_pinned_real_offline_ingestion_matches_native_plugins_media_and_steps(self):
        observations = {}
        output = ROOT / "test-results" / "tensorboard-gap-runtime.json"
        output.parent.mkdir(exist_ok=True)
        for mode in ("reference", "native"):
            env = {**os.environ, "WANDB_DATA_DIR": "/tmp/backprop-reference-data"}
            env["PYTHONPATH"] = str(ROOT / "sdk") if mode == "native" else ""
            env["BACKPROP_TENSORBOARD_PROBE_OUTPUT"] = str(output.with_name(
                "tensorboard-gap-runtime-" + mode + "-raw.json"))
            result = subprocess.run([sys.executable, str(ROOT / "tests/tensorboard_gap_runtime_probe.py"), mode],
                                    env=env, capture_output=True, text=True, timeout=180)
            output.with_name("tensorboard-gap-runtime-" + mode + "-process.json").write_text(json.dumps({
                "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}, indent=2) + "\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            observations[mode] = json.loads(result.stdout)
            output.write_text(json.dumps(observations, indent=2) + "\n")
        self.assertEqual(observations["reference"], observations["native"])
        self.assertEqual(observations["native"]["pr_curve"]["data"], [[.25, .75], [.75, .5], [0, 0]])

    def test_pinned_extended_images_and_legacy_histograms_match_exact_bytes(self):
        observations = {}
        output = ROOT / "test-results/tensorboard-gap-extended-media.json"
        output.parent.mkdir(exist_ok=True)
        for mode in ("reference", "native"):
            env = {**os.environ, "WANDB_DATA_DIR": "/tmp/backprop-reference-data",
                   "WANDB_CACHE_DIR": "/tmp/backprop-reference-cache"}
            env["PYTHONPATH"] = str(ROOT / "sdk") if mode == "native" else ""
            env["BACKPROP_TENSORBOARD_PROBE_OUTPUT"] = str(output.with_name(
                "tensorboard-gap-extended-media-" + mode + "-raw.json"))
            result = subprocess.run([sys.executable, str(ROOT / "tests/tensorboard_gap_runtime_probe.py"),
                                     mode, "extended-media"], env=env,
                                    capture_output=True, text=True, timeout=180)
            output.with_name("tensorboard-gap-extended-media-" + mode + "-process.json").write_text(
                json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                            "stderr": result.stderr}, indent=2) + "\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            observations[mode] = json.loads(result.stdout)
            output.write_text(json.dumps(observations, indent=2) + "\n")
        self.assertEqual(observations["reference"], observations["native"])
        self.assertEqual(observations["native"]["images"]["jpeg_frames"]["dimensions"], [[2, 3], [2, 3]])
        self.assertEqual(observations["native"]["images"]["gif_frames"]["count"], 1)

    def test_pinned_named_int64_scalars_use_float64_at_signed_boundaries(self):
        observations = {}
        output = ROOT / "test-results/tensorboard-gap-int64-boundaries.json"
        output.parent.mkdir(exist_ok=True)
        for mode in ("reference", "native"):
            env = {**os.environ, "WANDB_DATA_DIR": "/tmp/backprop-reference-data",
                   "WANDB_CACHE_DIR": "/tmp/backprop-reference-cache"}
            env["PYTHONPATH"] = str(ROOT / "sdk") if mode == "native" else ""
            env["BACKPROP_TENSORBOARD_PROBE_OUTPUT"] = str(output.with_name(
                "tensorboard-gap-int64-boundaries-" + mode + "-raw.json"))
            result = subprocess.run([sys.executable, str(ROOT / "tests/tensorboard_gap_runtime_probe.py"),
                                     mode, "int64-boundaries"], env=env,
                                    capture_output=True, text=True, timeout=180)
            output.with_name("tensorboard-gap-int64-boundaries-" + mode + "-process.json").write_text(
                json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                            "stderr": result.stderr}, indent=2) + "\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            observations[mode] = json.loads(result.stdout)
            output.write_text(json.dumps(observations, indent=2) + "\n")
        boundaries = [2**53 - 1, 2**53, 2**53 + 1, -(2**53 - 1), -2**53, -(2**53 + 1)]
        self.assertEqual(observations["reference"], observations["native"])
        self.assertEqual(observations["native"]["scalars"], list(map(float, boundaries)))
        self.assertEqual(observations["native"]["global_steps"], boundaries)

    def test_pinned_graph_metadata_and_unknown_plugins_have_no_conversion(self):
        observations = {}
        output = ROOT / "test-results/tensorboard-gap-unsupported-boundaries.json"
        output.parent.mkdir(exist_ok=True)
        for mode in ("reference", "native"):
            env = {**os.environ, "WANDB_DATA_DIR": "/tmp/backprop-reference-data",
                   "WANDB_CACHE_DIR": "/tmp/backprop-reference-cache"}
            env["PYTHONPATH"] = str(ROOT / "sdk") if mode == "native" else ""
            env["BACKPROP_TENSORBOARD_PROBE_OUTPUT"] = str(output.with_name(
                "tensorboard-gap-unsupported-boundaries-" + mode + "-raw.json"))
            result = subprocess.run([sys.executable, str(ROOT / "tests/tensorboard_gap_runtime_probe.py"),
                                     mode, "unsupported-boundaries"], env=env,
                                    capture_output=True, text=True, timeout=240)
            output.with_name("tensorboard-gap-unsupported-boundaries-" + mode + "-process.json").write_text(
                json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                            "stderr": result.stderr}, indent=2) + "\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            observations[mode] = json.loads(result.stdout)
            output.write_text(json.dumps(observations, indent=2) + "\n")
        for name in ("graph", "run_metadata", "hparams", "unknown"):
            self.assertEqual(observations["reference"][name]["metrics"], {})
            self.assertIsNone(observations["reference"][name]["error"])
            self.assertEqual(observations["native"][name]["error"], "NotImplementedError")
            self.assertEqual(observations["native"][name]["metrics"], {})

    def test_supported_plugins_omitted_metadata_and_portable_media(self):
        directory = Path(self.tmp.name) / "events"
        write(directory)
        run = self.run_for()
        self.assertEqual(tb.sync(run, directory), 2)
        rows = self.rows(run)
        self.assertEqual([row["global_step"] for row in rows], [8, 9])
        self.assertEqual([row["_timestamp"] for row in rows], [100, 101])
        self.assertEqual([row["loss"] for row in rows], [.25, .125])
        self.assertEqual(rows[0]["weights"]["values"], [2, 3])
        self.assertEqual(rows[1]["weights"]["values"], [4, 1])
        self.assertEqual(rows[0]["legacy_weights"]["bins"], [0, 1, 2])
        self.assertEqual(rows[0]["legacy_weights"]["values"], [2, 3])
        image = rows[0]["frame"][0]
        self.assertEqual(self.sink.download_file(image), png())
        self.assertEqual(rows[0]["curve"]["title"], "curve Precision v. Recall")
        table = rows[0]["curve"]["table"]
        self.assertEqual(table["columns"], ["recall", "precision"])
        self.assertEqual(table["data"], [[.25, .75], [.75, .5], [0, 0]])
        self.assertTrue(any(event["type"] == "file" for event in self.sink.events))
        self.assertEqual(tb.sync(run, directory), 0)

    def test_restarts_copies_late_files_reordered_steps_and_independent_writers(self):
        from tensorboard.compat.proto.event_pb2 import Event
        from tensorboard.compat.proto.summary_pb2 import Summary
        source = Path(self.tmp.name) / "source"
        later = Event(step=8, wall_time=200., summary=Summary(value=[Summary.Value(tag="metric", simple_value=2)]))
        first_file = write(source, [later, later])
        run = self.run_for()
        self.assertEqual(tb.sync(run, source), 1)
        run.flush()
        run.finish()
        moved = Path(self.tmp.name) / "copied"
        shutil.copytree(source, moved)
        # A different local journal simulates a fresh machine; visible backend
        # history, rather than an old in-memory set, provides the replay keys.
        with patch.dict(os.environ, {"BACKPROP_DIR": self.tmp.name + "/restart"}):
            recovered = self.run_for(resume="allow")
        self.assertEqual(tb.sync(recovered, moved), 0)
        older = Event(step=2, wall_time=100., summary=Summary(value=[Summary.Value(tag="metric", simple_value=1)]))
        late_dir = Path(self.tmp.name) / "late"
        late_file = write(late_dir, [older])
        late_destination = moved / "events.out.tfevents.1.late-machine.1"
        shutil.copyfile(late_file, late_destination)
        self.assertEqual(tb.sync(recovered, moved), 1)
        self.assertEqual(tb.sync(recovered, moved), 0)
        independent = moved / "events.out.tfevents.2.independent-machine.1"
        shutil.copyfile(first_file, independent)
        # One new writer group and one revised next-file barrier are durable
        # physical revisions; public source replay still exposes three rows.
        self.assertEqual(tb.sync(recovered, moved), 2)
        rows = self.rows(recovered)
        self.assertEqual([row["global_step"] for row in rows], [2, 8, 8])
        self.assertEqual(len({row["_tensorboard_record"] for row in rows}), 3)

    def test_patch_switches_save_namespaces_cleanup_and_repeated_sessions(self):
        writer_module = importlib.import_module("tensorboard.summary.writer.event_file_writer")
        original = writer_module.EventFileWriter.__init__
        run = self.run_for()
        root = Path(self.tmp.name) / "tree"
        tb.patch(save=False, root_logdir=str(root), pytorch=True, tensorboard_x=True)
        with self.assertRaisesRegex(ValueError, "already patched"):
            tb.patch()
        write(root / "train", events()[:1])
        write(root / "validation", events()[:1])
        self.assertEqual(len(tb.directories()), 2)
        self.assertEqual(tb.sync(run, root), 2)
        rows = self.rows(run)
        self.assertIn("train/loss", rows[0])
        self.assertIn("validation/loss", rows[1])
        self.assertFalse(any(event["type"] == "file" and "tfevents" in event["payload"]["name"]
                             for event in self.sink.events))
        # save=False suppresses source event-file uploads; conversion still
        # publishes usable media and PR chart data alongside ordinary run files.
        for namespace, row in zip(("train", "validation"), rows):
            self.assertEqual(self.sink.download_file(row[namespace + "/frame"][0]), png())
            self.assertEqual(row[namespace + "/curve"]["table"]["data"], [[.25, .75], [.75, .5], [0, 0]])
        tb.unpatch()
        self.assertIs(writer_module.EventFileWriter.__init__, original)
        self.assertEqual(tb.directories(), [])
        tb.unpatch()
        tb.patch(save=True, root_logdir=str(root))
        write(root / "again", events()[:1])
        self.assertEqual(tb.directories(), [str((root / "again").resolve())])
        tb.unpatch()
        self.assertIs(writer_module.EventFileWriter.__init__, original)

    def test_api_import_ignores_live_patch_root_and_restores_all_historical_sources(self):
        directory = Path(self.tmp.name) / "tree" / "train"
        tb.patch(save=False, root_logdir=str(directory.parent))
        current = write(directory, events()[:1])
        old = directory / "events.out.tfevents.1.other-machine.1"
        current.rename(old)
        api = Api._from_store(self.sink, entity="e", project="tb-gap")
        result = api.sync_tensorboard(directory, run_id="api-historical")
        rows = list(result.scan_history())
        self.assertEqual(len(rows), 1)
        self.assertIn("loss", rows[0])
        self.assertNotIn("train/loss", rows[0])
        self.assertTrue(any("tfevents" in file.name for file in result.files()))
        self.assertTrue(tb._patches)

    def test_auto_init_flags_multiple_runs_and_finish_ownership(self):
        """Separate writer routing from the lifetime of automatic patch leases.

        Main caller: genuine optional writer CI. Pinned writer discovery targets
        only the global run; create_new owns hooks without binding that global.
        Why this exists: finishing a nonglobal run cannot manufacture imported
        rows merely because its hook lease was the last one still alive.
        """
        observations = []
        for keyword in ("sync_tensorboard", "tensorboard"):
            for bound_first in (True, False):
                with self.subTest(keyword=keyword, bound_first=bound_first):
                    suffix = keyword + ("-bound" if bound_first else "-nonglobal")
                    self.assertIsNone(backprop.run)
                    first = backprop.init(id="first-" + suffix, entity="e", project="tb-gap", dir=self.tmp.name,
                        settings=SETTINGS, _sink=self.sink,
                        reinit="finish_previous" if bound_first else "create_new", **{keyword: True})
                    self.runs.append(first)
                    second = backprop.init(id="second-" + suffix, entity="e", project="tb-gap", dir=self.tmp.name,
                        settings=SETTINGS, _sink=self.sink, reinit="create_new", **{keyword: True})
                    self.runs.append(second)
                    self.assertIs(backprop.run, first if bound_first else None)
                    self.assertIn(first, tb._registered_runs)
                    self.assertIn(second, tb._registered_runs)
                    source = write(Path(self.tmp.name) / suffix, events()[:1])
                    first.finish()
                    self.assertTrue(tb._patches)
                    self.assertNotIn(first, tb._registered_runs)
                    self.assertIn(second, tb._registered_runs)
                    first_rows = self.rows(first)
                    second_rows_before_finish = self.rows(second)
                    second.finish()
                    second_rows_after_finish = self.rows(second)
                    observations.append({
                        "keyword": keyword, "bound_first": bound_first,
                        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "first_history": first_rows,
                        "second_history_before_finish": second_rows_before_finish,
                        "second_history_after_finish": second_rows_after_finish,
                        "physical_history": [event for event in self.sink.events
                            if event["type"] == "history" and event["run_id"] in (first.id, second.id)]})
                    output = ROOT / "test-results/tensorboard-gap-init-routing-native-raw.json"
                    output.parent.mkdir(exist_ok=True)
                    output.write_text(json.dumps(observations, indent=2) + "\n")
                    self.assertEqual(second_rows_before_finish, [])
                    self.assertEqual(second_rows_after_finish, [])
                    if bound_first:
                        self.assertEqual(len(first_rows), 1)
                        self.assertEqual(first_rows[0]["loss"], .25)
                        self.assertEqual(first_rows[0]["global_step"], 8)
                        self.assertEqual(self.sink.download_file(first_rows[0]["frame"][0]), png())
                        self.assertEqual(first_rows[0]["curve"]["table"]["data"],
                                         [[.25, .75], [.75, .5], [0, 0]])
                    else:
                        self.assertEqual(first_rows, [])
                    self.assertFalse(tb._patches)
                    self.assertNotIn(second, tb._registered_runs)
                    self.assertIsNone(backprop.run)
        tb.patch(save=False)
        manual = backprop.init(id="manual", entity="e", project="tb-gap", dir=self.tmp.name,
                               settings=SETTINGS, _sink=self.sink, sync_tensorboard=True, reinit="create_new")
        self.runs.append(manual)
        manual.finish()
        self.assertTrue(tb._patches)
        tb.release_run(manual)
        self.assertTrue(tb._patches)

    def test_manual_patch_after_init_adopts_current_run_only(self):
        for index, flag in enumerate((None, False)):
            tb.unpatch()
            current = backprop.init(id="patch-after-init-" + str(index), entity="e", project="tb-gap",
                dir=self.tmp.name, settings=SETTINGS, _sink=self.sink,
                sync_tensorboard=flag, reinit="finish_previous")
            self.runs.append(current)
            independent = backprop.init(id="nonglobal-before-patch-" + str(index), entity="e", project="tb-gap",
                dir=self.tmp.name, settings=SETTINGS, _sink=self.sink,
                sync_tensorboard=False, reinit="create_new")
            self.runs.append(independent)
            self.assertIs(backprop.run, current)
            self.assertFalse(current._tensorboard_registered)
            self.assertFalse(independent._tensorboard_registered)
            root = Path(self.tmp.name) / ("patch-after-init-" + str(index))
            tb.patch(save=False, root_logdir=str(root))
            write(root, events()[:1])
            self.assertTrue(current._tensorboard_registered)
            self.assertFalse(current.settings.sync_tensorboard)
            self.assertFalse(independent._tensorboard_registered)
            current.finish()
            self.assertEqual(len(self.rows(current)), 1)
            self.assertEqual(self.rows(independent), [])
            self.assertTrue(tb.is_patched())
            independent.finish()
            self.assertTrue(tb.is_patched())
            tb.unpatch()
            self.assertFalse(tb.is_patched())
    def test_patch_failure_restores_originals_and_live_filename_filter(self):
        from tensorboard.compat.proto.event_pb2 import Event
        from tensorboard.compat.proto.summary_pb2 import Summary
        writer_module = importlib.import_module("tensorboard.summary.writer.event_file_writer")
        original = writer_module.EventFileWriter.__init__
        original_import = tb._optional_module
        def fail_after_first_writer(name):
            if name == "torch.utils.tensorboard.writer":
                raise RuntimeError("installed framework import failed")
            return original_import(name)
        with patch.object(tb, "_optional_module", side_effect=fail_after_first_writer):
            with self.assertRaisesRegex(RuntimeError, "installed framework"):
                tb.patch()
        self.assertIs(writer_module.EventFileWriter.__init__, original)
        self.assertEqual(tb._patches, [])
        run = self.run_for("live")
        tb.register_run(run)
        directory = Path(self.tmp.name) / "live-source"
        current = write(directory, [Event(step=1, summary=Summary(value=[Summary.Value(tag="metric", simple_value=1)]))])
        shutil.copyfile(current, directory / "events.out.tfevents.1.old-machine.1")
        import socket
        shutil.copyfile(current, directory / ("events.out.tfevents.1." + socket.gethostname() + ".1"))
        self.assertEqual(tb.sync(run, directory, final=True), 1)
        self.assertEqual(len(self.rows(run)), 1)
        tb.release_run(run)

    def test_errors_unsupported_plugins_shapes_and_source_paths(self):
        from tensorboard.compat.proto.event_pb2 import Event
        from tensorboard.compat.proto.summary_pb2 import Summary
        for value, error in ((tensor_value("bad", 1, "unknown"), NotImplementedError),
                             (tensor_value("bad", [1, 2], "scalars"), ValueError),
                             (tensor_value("bad", [1, 2], "histograms"), ValueError),
                             (Summary.Value(tag="", simple_value=1), ValueError)):
            with self.assertRaises(error):
                tb.values(Summary(value=[value]))
        run = self.run_for()
        with self.assertRaises(FileNotFoundError):
            tb.sync(run, Path(self.tmp.name) / "missing")
        write(Path(self.tmp.name) / "graph", [Event(graph_def=b"unsupported")])
        with self.assertRaisesRegex(NotImplementedError, "graph"):
            tb.sync(run, Path(self.tmp.name) / "graph")

    @unittest.skipUnless(importlib.util.find_spec("tensorflow") is not None, "optional TensorFlow is not installed")
    def test_tf2_native_writer_interception_and_pytorch_switch(self):
        import tensorflow as tf
        from tensorflow.python.ops import gen_summary_ops
        original = gen_summary_ops.create_summary_file_writer
        run = self.run_for()
        directory = str(Path(self.tmp.name) / "tf2")
        tb.patch(save=False, root_logdir=directory)
        writer = tf.summary.create_file_writer(directory)
        with writer.as_default():
            tf.summary.scalar("tf_loss", .5, step=3)
            tf.summary.histogram("tf_weights", [1., 2., 3.], step=3)
            tf.summary.image("tf_image", tf.ones([1, 2, 3, 1]), step=3)
        writer.flush()
        writer.close()
        self.assertIn(str(Path(directory).resolve()), tb.directories())
        self.assertGreaterEqual(tb.sync(run, directory), 1)
        self.assertIn("tf_loss", {key for row in self.rows(run) for key in row})
        tb.unpatch()
        self.assertIs(gen_summary_ops.create_summary_file_writer, original)
        tb.patch(pytorch=True)
        self.assertIs(gen_summary_ops.create_summary_file_writer, original)

    @unittest.skipUnless(importlib.util.find_spec("torch") is not None, "optional PyTorch is not installed")
    def test_pytorch_writer_interception(self):
        from torch.utils.tensorboard import SummaryWriter
        run = self.run_for()
        tb.patch(save=False, pytorch=True)
        directory = str(Path(self.tmp.name) / "pytorch")
        writer = SummaryWriter(directory)
        writer.add_scalar("torch_loss", .3, 4)
        writer.flush()
        writer.close()
        self.assertIn(str(Path(directory).resolve()), tb.directories())
        self.assertEqual(tb.sync(run, directory), 1)
        self.assertAlmostEqual(self.rows(run)[0]["torch_loss"], .3)

    @unittest.skipUnless(importlib.util.find_spec("tensorboardX") is not None, "optional TensorBoardX is not installed")
    def test_tensorboard_x_writer_interception(self):
        from tensorboardX import SummaryWriter
        run = self.run_for()
        tb.patch(save=False, tensorboard_x=True)
        directory = str(Path(self.tmp.name) / "tbx")
        writer = SummaryWriter(directory)
        writer.add_scalar("tbx_loss", .3, 4)
        writer.flush()
        writer.close()
        self.assertIn(str(Path(directory).resolve()), tb.directories())
        self.assertEqual(tb.sync(run, directory), 1)
        self.assertAlmostEqual(self.rows(run)[0]["tbx_loss"], .3)


if __name__ == "__main__":
    unittest.main()
