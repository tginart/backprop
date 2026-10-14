"""Source identities and cloud reads checked without hosted services.

Architecture: portable identity/snapshot checks -> actual optional gfile and
EventFileWriter paths -> independent journals, encoded media and public history.
Main callers: test_optional_*.py CI discovery. Only cloud provider I/O and
delayed backend visibility are replaced; conversion and persistence remain real.
Missing TensorBoard stays a skip and authenticated providers remain unverified.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import backprop
from backprop.apis.public import Api
from backprop.integration import tensorboard as tb
from backprop.integration.tensorboard import origin, sources
from backprop.integration.tensorboard.reader import _record_keys
from tensorboard_gap_cloud import OfflineGFileBoundary, MemoryBlobProvider, registered_provider
from tensorboard_gap_fixtures import encoded_image, events, png, write, delayed_duplicate_import

HAS_TB = importlib.util.find_spec("tensorboard") is not None
HAS_MEDIA = importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("numpy") is not None
ROOT = Path(__file__).resolve().parents[1]
SETTINGS = {"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True,
            "flush_interval": 3600}


class TensorBoardSourcePortableContracts(unittest.TestCase):
    def test_versioned_origin_digest_vector_and_strict_sidecars(self):
        sidecar = origin.event_origin("train", "events.out.tfevents.1.host.2", b"event")
        key = origin.record_identity_from_origin(sidecar)
        self.assertEqual(key, "4cba61ad3d9f80ff09bbea2740af3436b69aff4f90f5bcb12894563dd910bbe6")
        self.assertEqual(sidecar["event_sha256"], hashlib.sha256(b"event").hexdigest())
        event = {"type": "history", "payload": {"metrics": {
            "_tensorboard_record": key, "_tensorboard_origin": sidecar}}}
        self.assertEqual(_record_keys([event]), {key})
        old = {"type": "history", "payload": {"metrics": {"_tensorboard_record":
            origin.prior_record_identity("train", sidecar["filename"], b"event")}}}
        self.assertEqual(_record_keys([old]), {old["payload"]["metrics"]["_tensorboard_record"]})
        for field, value in (("version", 2), ("version", True), ("namespace", "bad\0label"),
                             ("filename", "../file"), ("filename", ""), ("filename", "."),
                             ("event_sha256", "A" * 64), ("occurrence", -1),
                             ("occurrence", True), ("occurrence", 2**53), ("occurrence", 1.0)):
            malformed = {**sidecar, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                origin.record_identity_from_origin(malformed)
        for malformed in ({**sidecar, "extra": 1}, {key: value for key, value in sidecar.items() if key != "version"}):
            with self.assertRaises(ValueError):
                origin.record_identity_from_origin(malformed)
        bad = copy.deepcopy(event)
        bad["payload"]["metrics"]["_tensorboard_record"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            _record_keys([bad])

    def test_cloud_paths_and_interrupted_snapshots_are_atomic(self):
        for path in ("gs://other/root/file", "gs://bucket/root/../file", "gs://bucket/root//file",
                     "gs://bucket/root/./file", "gs://bucket/root/file\0tail"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                sources.relative_source(path, "gs://bucket/root")
        self.assertEqual(sources.relative_source("gs://bucket/root/train/encoded%2Fname", "gs://bucket/root"),
                         "train/encoded%2Fname")
        provider = OfflineGFileBoundary(b"complete-event-source")
        with tempfile.TemporaryDirectory() as directory, patch.object(sources, "filesystem", return_value=provider):
            run = SimpleNamespace(sync_dir=directory)
            local, cache = sources.snapshot(run, "gs://bucket/root/train/events.out.tfevents.1.host", "gs://bucket/root")
            self.assertEqual(Path(local).read_bytes(), provider.data)
            provider.data = b"new-incomplete-source"
            provider.fail_after_first_chunk = True
            with self.assertRaisesRegex(OSError, "interrupted"):
                sources.snapshot(run, "gs://bucket/root/train/events.out.tfevents.1.host", "gs://bucket/root")
            self.assertEqual(Path(local).read_bytes(), b"complete-event-source")
            with self.assertRaises(OSError):
                sources.snapshot(run, "gs://bucket/root/train/events.out.tfevents.2.host", "gs://bucket/root")
            self.assertFalse((Path(cache) / "train/events.out.tfevents.2.host").exists())
            self.assertFalse(list(Path(cache).rglob(".tensorboard-copy-*")))
            self.assertEqual(provider.reads, 3)

    @unittest.skipUnless(HAS_MEDIA, "optional Pillow/NumPy are not installed")
    def test_jpeg_dimensions_gif_bytes_and_mixed_batch_errors(self):
        from backprop.integration.tensorboard.codec import _images, _histogram
        for data in (encoded_image("JPEG"), encoded_image("GIF", animated=True)):
            image = _images([data], 2, 3)[0]
            self.assertEqual(image.metadata["width"], 2)
            self.assertEqual(image.metadata["height"], 3)
            self.assertEqual(image._bytes(), data)
        with self.assertRaisesRegex(ValueError, "one encoded format"):
            _images([png(), encoded_image("JPEG")], 2, 3)
        with self.assertRaisesRegex(ValueError, "dimensions"):
            _images([encoded_image("JPEG")], 3, 2)
        self.assertEqual(_histogram([], [0]).histogram, [])
        self.assertEqual(_histogram([], [0]).bins, [0])

    def test_imported_large_pr_chart_bytes_match_independent_stores(self):
        key = origin.record_identity("train", "events.out.tfevents.1.host.2", b"event")
        observations = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
            for label in ("a", "b"):
                sink = backprop.MemorySink()
                table = backprop.Table(columns=["recall", "precision"],
                    data=[[index / 6001, (6000 - index) / 6001] for index in range(6000)])
                chart = backprop.plot_table("wandb/line/v0", table,
                    {"x": "recall", "y": "precision"}, {"title": "train/curve Precision v. Recall"})
                origin.stabilize_table_ids({"train/curve": chart}, key)
                self.assertEqual(len(table._tensorboard_record_identity), 64)
                self.assertEqual(table._id, table._tensorboard_record_identity[:32])
                with backprop.Run(id="large-pr", entity="e", project="tb-gap", mode="shared",
                                  dir=directory, _sink=sink, _deployment={},
                                  settings={**SETTINGS, "x_label": label, "x_primary": False}) as run:
                    run.log({"train/curve": chart})
                event = next(event for event in sink.events if event["type"] == "history")
                reference = event["payload"]["metrics"]["train/curve"]["table"]
                self.assertTrue(reference["external"])
                encoded = sink.download_file(reference)
                self.assertGreater(len(encoded), 128_000)
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), reference["sha256"])
                self.assertEqual(len(encoded), reference["size"])
                observations.append((reference["sha256"], encoded, table._id))
            self.assertEqual(observations[0], observations[1])
            other = backprop.Table(columns=["recall"], data=[[0]])
            origin.stabilize_table_ids({"other": other}, key)
            self.assertNotEqual(other._id, observations[0][2])


@unittest.skipUnless(HAS_TB and HAS_MEDIA, "optional tensorboard/Pillow/NumPy are not installed")
class TensorBoardCloudSourceContracts(unittest.TestCase):
    def setUp(self):
        tb.unpatch()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"BACKPROP_DIR": self.tmp.name})
        self.env.start()
        self.runs = []

    def tearDown(self):
        for run in self.runs:
            run.finish()
        tb.unpatch()
        self.env.stop()
        self.tmp.cleanup()

    def test_real_registered_cloud_filesystems_preserve_namespace_bytes_and_restart_identity(self):
        local = write(Path(self.tmp.name) / "fixture")
        data = local.read_bytes()
        for index, root in enumerate(("s3://bucket/logs", "gs://bucket/logs", "az://account/container/logs")):
            with self.subTest(root=root):
                provider = MemoryBlobProvider({root + "/train/" + local.name: data})
                sink = backprop.MemorySink()
                run = backprop.Run(id="cloud-" + str(index), entity="e", project="tb-gap", dir=self.tmp.name,
                                   settings=SETTINGS, _sink=sink, _deployment={})
                self.runs.append(run)
                with registered_provider(provider) as filesystem, patch.object(sources, "filesystem", return_value=filesystem):
                    self.assertEqual(tb.sync(run, root), 2)
                    run.flush()
                    rows = list(Api._from_store(sink, entity="e", project="tb-gap").run(run.id).scan_history())
                    self.assertEqual([row["train/loss"] for row in rows], [.25, .125])
                    self.assertEqual(sink.download_file(rows[0]["train/frame"][0]), png())
                    saved = next(event["payload"] for event in sink.events if event["type"] == "file"
                                 and "tfevents" in event["payload"]["name"])
                    self.assertEqual(sink.download_file(saved["file"]), data)
                    self.assertEqual(tb.sync(run, root), 0)
                    run.finish()
                    with patch.dict(os.environ, {"BACKPROP_DIR": self.tmp.name + "/restart"}):
                        resumed = backprop.Run(id=run.id, entity="e", project="tb-gap", dir=self.tmp.name,
                            resume="allow", settings=SETTINGS, _sink=sink, _deployment={})
                    self.runs.append(resumed)
                    copied = root + "-copied"
                    provider.objects[copied + "/train/" + local.name] = data
                    self.assertEqual(tb.sync(resumed, copied), 0)
                    provider.objects[copied + "/train/events.out.tfevents.1.independent.1"] = data
                    self.assertEqual(tb.sync(resumed, copied), 2)
                    late = write(Path(self.tmp.name) / ("late-" + str(index)), events()[:1]).read_bytes()
                    provider.objects[copied + "/validation/events.out.tfevents.1.late.1"] = late
                    self.assertEqual(tb.sync(resumed, copied), 1)
                    provider.failure = PermissionError("provider denied read")
                    with self.assertRaisesRegex(PermissionError, "denied"):
                        tb.sync(resumed, copied)
                    provider.failure = None
                    self.assertEqual(tb.sync(resumed, copied), 0)
                    self.assertGreater(len(provider.reads), 0)

    def test_cloud_manual_patch_save_false_retains_converted_media(self):
        local = write(Path(self.tmp.name) / "fixture", events()[:1])
        root = "gs://bucket/logs"
        provider = MemoryBlobProvider({root + "/train/" + local.name: local.read_bytes()})
        sink = backprop.MemorySink()
        run = backprop.Run(id="cloud-save-false", entity="e", project="tb-gap", dir=self.tmp.name,
                           settings=SETTINGS, _sink=sink, _deployment={})
        self.runs.append(run)
        # This direct handle imports historical fixture bytes manually. Install
        # the policy after construction so live writer filtering does not turn
        # an earlier fixture into an ignored source; global adoption is covered
        # independently by the patch-after-init writer lifecycle test.
        tb.patch(save=False, root_logdir=root)
        with registered_provider(provider) as filesystem, patch.object(sources, "filesystem", return_value=filesystem):
            self.assertEqual(tb.sync(run, root), 1)
        run.flush()
        rows = list(Api._from_store(sink, entity="e", project="tb-gap").run(run.id).scan_history())
        self.assertEqual(sink.download_file(rows[0]["train/frame"][0]), png())
        self.assertFalse(any(event["type"] == "file" and "tfevents" in event["payload"]["name"]
                             for event in sink.events))
        tb.unpatch()
        self.assertFalse(tb.is_patched())

    def test_hidden_duplicate_imports_have_actual_equal_source_and_media_evidence(self):
        from backprop.apis.public.replay import reduce_events
        output = ROOT / "test-results/tensorboard-gap-delayed-duplicates.json"
        output.parent.mkdir(exist_ok=True)
        for large in (False, True):
            target = output.with_name("tensorboard-gap-delayed-duplicates-large.json") if large else output
            history = delayed_duplicate_import(target, large_pr=large)
            self.assertEqual(len(history), 2)
            first, second = [event["payload"]["metrics"] for event in history]
            self.assertEqual(first["_tensorboard_origin"], second["_tensorboard_origin"])
            self.assertEqual(origin.validate_record_origin(first["_tensorboard_record"], first["_tensorboard_origin"]),
                             second["_tensorboard_record"])
            self.assertEqual(first["frame"][0]["sha256"], second["frame"][0]["sha256"])
            if large:
                self.assertTrue(first["curve"]["table"]["external"])
                self.assertEqual(first["curve"]["table"]["sha256"], second["curve"]["table"]["sha256"])
                self.assertGreater(first["curve"]["table"]["size"], 128_000)
            else:
                self.assertEqual(first["curve"]["table"]["table_id"], second["curve"]["table"]["table_id"])
                self.assertEqual(first["curve"]["table"]["artifact_path"], second["curve"]["table"]["artifact_path"])
            observation = json.loads(target.read_text())
            self.assertEqual(len(observation["stores"]), 2)
            self.assertEqual(observation["expected_reconstructed_history_count"], 1)
            for store in observation["stores"]:
                sequences = [event["payload"]["_writer"]["sequence"] for event in store["events"]]
                self.assertEqual(sequences, list(range(len(sequences))))
            visible = backprop.MemorySink()
            visible.events = observation["events"]
            visible.files = {name: bytes.fromhex(data) for name, data in observation["files"].items()}
            rows = list(Api._from_store(visible, entity="e", project="tb-gap").run("tb-duplicate").scan_history())
            self.assertEqual(len(rows), 1)
            self.assertEqual(visible.download_file(rows[0]["frame"][0]), png())
            self.assertEqual(len(rows[0]["curve"]["table"]["data"]), 6000 if large else 3)
            physical = observation["events"]
            for delivery in (physical, list(reversed(physical)), physical * 2):
                state = reduce_events(delivery)
                self.assertEqual(len(state["history"]), 1)
                self.assertEqual(len(state["writers"]), 2)
                self.assertEqual({writer["state"] for writer in state["writers"].values()}, {"finished"})
            self.assertEqual(physical, json.loads(target.read_text())["events"])


if __name__ == "__main__":
    unittest.main()
