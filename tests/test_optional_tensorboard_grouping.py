"""Grouped-source arithmetic and real optional TensorBoard comparisons.

Architecture: portable provenance/group/encoder checks -> genuine writer
fixtures -> isolated pinned offline-core comparison and durable native replay.
Main callers: existing test_optional_*.py CI discovery. Fabricated byte labels
in portable checks exercise protocol arithmetic only, never plugin parity.
Actual writer comparisons remain explicit skips when TensorBoard is absent.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import backprop
from backprop.integration.tensorboard.grouping import closed_groups, group_revision, history_metrics, validate_group, validate_group_origin
from backprop.integration.tensorboard.origin import event_origin, stabilize_table_ids, record_identity_from_origin
from backprop.integration.tensorboard import sources

ROOT = Path(__file__).resolve().parents[1]
HAS_TB = importlib.util.find_spec("tensorboard") is not None
HAS_MEDIA = importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("numpy") is not None


def observation(index, step, metrics=None, *, filename="events.out.tfevents.1.host", wall_time=None):
    """Build source-backed protocol arithmetic, separate from actual writers."""
    raw = json.dumps([filename, index, step, wall_time], separators=(",", ":")).encode()
    return {"origin": event_origin("", filename, raw), "index": index, "step": str(step),
            "wall_time": index + 100. if wall_time is None else wall_time, "metrics": metrics or {}}


def visible_values(group):
    metrics = history_metrics(group)
    return {key: value for key, value in metrics.items() if not key.startswith("_tensorboard")}


class TensorBoardGroupingPortableContracts(unittest.TestCase):
    def test_finalized_group_readback_requires_explicit_api_snapshot_refresh(self):
        """Separate durable producer publication from cached public readback.

        Main callers: optional CI and the historical TensorBoard import test.
        Publishing a finalized group does not mutate a previously read public
        Run. Clearing the API cache creates a fresh snapshot; forcing load can
        separately refresh the original handle. This storage boundary needs no
        optional TensorBoard event parser.
        """
        from backprop.apis.public import Api
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
            sink = backprop.MemorySink()
            with backprop.Run(id="snapshot", entity="e", project="tb-group", dir=directory,
                _sink=sink, _deployment={}, settings={"silent": True, "console": "off",
                    "disable_git": True, "x_disable_stats": True, "flush_interval": 3600}) as run:
                run.flush()
                api = Api._from_store(sink, entity="e", project="tb-group")
                pending = api.run(run.id)
                self.assertNotIn("loss", pending.summary)
                source = [observation(0, 0), observation(1, 12, {"loss": .25})]
                run.log(history_metrics(list(closed_groups(source, final=True))[0]))
                run.flush()
                self.assertTrue(any(event["type"] == "history" and event["payload"]["metrics"].get("loss") == .25
                    for event in sink.events))
                self.assertIs(api.run(run.id), pending)
                self.assertNotIn("loss", pending.summary)
                api.flush()
                fresh = api.run(run.id)
                self.assertIsNot(fresh, pending)
                self.assertEqual(fresh.summary["loss"], .25)
                self.assertEqual(fresh.summary["global_step"], 12)
                self.assertEqual(len(list(fresh.scan_history())), 1)
                self.assertNotIn("loss", pending.summary)
                pending.load(force=True)
                self.assertEqual(pending.summary["loss"], .25)
                self.assertEqual(len(list(pending.scan_history())), 1)

    def test_fixture_png_has_valid_checksums_without_pillow(self):
        """Verify the fixed PNG through its standard byte-format boundaries."""
        import struct
        import zlib
        from tensorboard_grouping_replay_fixture import PNG_BYTES
        self.assertEqual(PNG_BYTES[:8], b"\x89PNG\r\n\x1a\n")
        offset, chunks = 8, []
        while offset < len(PNG_BYTES):
            length = struct.unpack(">I", PNG_BYTES[offset:offset + 4])[0]
            kind = PNG_BYTES[offset + 4:offset + 8]
            data = PNG_BYTES[offset + 8:offset + 8 + length]
            checksum = struct.unpack(">I", PNG_BYTES[offset + 8 + length:offset + 12 + length])[0]
            self.assertEqual(zlib.crc32(kind + data), checksum)
            chunks.append((kind, data))
            offset += length + 12
        self.assertEqual(offset, len(PNG_BYTES))
        self.assertEqual([kind for kind, _ in chunks], [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(struct.unpack(">IIBBBBB", chunks[0][1]), (2, 3, 8, 2, 0, 0, 0))
        scanlines = zlib.decompress(chunks[1][1])
        self.assertEqual(len(scanlines), 3 * (1 + 2 * 3))
        previous = bytes(6)
        for offset in range(0, len(scanlines), 7):
            filter_kind = scanlines[offset]
            self.assertIn(filter_kind, (1, 2))
            row = bytearray(scanlines[offset + 1:offset + 7])
            for index in range(6):
                predictor = row[index - 3] if filter_kind == 1 and index >= 3 else 0
                if filter_kind == 2:
                    predictor = previous[index]
                row[index] = (row[index] + predictor) % 256
            self.assertEqual(row, bytes((17, 34, 51)) * 2)
            previous = row

    def test_repeated_tags_sparse_values_empty_members_and_exact_timestamp(self):
        source = [observation(0, 0), observation(1, 5, {"loss": 1.}),
                  observation(2, 5, {"accuracy": .75}), observation(3, 5, {"loss": 2.}),
                  observation(4, 5), observation(5, 3, {"loss": 4.})]
        groups = list(closed_groups(source, final=True))
        self.assertEqual([visible_values(group) for group in groups], [
            {"loss": 2., "accuracy": .75, "global_step": 5, "_timestamp": 104.},
            {"loss": 4., "global_step": 3, "_timestamp": 105.}])
        self.assertEqual(groups[0]["prefix"], source[:1])
        self.assertEqual([member["index"] for member in groups[0]["members"]], [1, 2, 3, 4])
        self.assertEqual(groups[0]["next"]["index"], 5)
        self.assertNotIn("metrics", groups[0]["next"])
        self.assertIsNone(groups[1]["next"])

    def test_live_tail_restart_rescan_and_late_same_step_append(self):
        source = [observation(0, 0), observation(1, 8, {"loss": 1.})]
        self.assertEqual(list(closed_groups(source, final=False)), [])
        initial = list(closed_groups(source, final=True))[0]
        source += [observation(2, 8, {"sparse": 2.}), observation(3, 8, {"loss": 3.})]
        self.assertEqual(list(closed_groups(source, final=False)), [])
        revised = list(closed_groups(source, final=True))[0]
        self.assertEqual(initial["members"][0]["origin"], revised["members"][0]["origin"])
        self.assertNotEqual(initial["revision"], revised["revision"])
        self.assertEqual(visible_values(revised), {"loss": 3., "sparse": 2., "global_step": 8, "_timestamp": 103.})
        source.append(observation(4, 9, {"loss": 4.}))
        closed = list(closed_groups(copy.deepcopy(source), final=False))
        self.assertEqual(len(closed), 1)
        self.assertEqual(visible_values(closed[0]), visible_values(revised))
        self.assertNotEqual(closed[0]["revision"], revised["revision"])

    def test_file_headers_are_real_step_boundaries_and_step_zero_can_span_files(self):
        first, second = "events.out.tfevents.1.a", "events.out.tfevents.2.b"
        for step, expected in ((5, 2), (0, 1)):
            with self.subTest(step=step):
                source = [observation(0, 0, filename=first), observation(1, step, {"a": 1}, filename=first),
                          observation(0, 0, filename=second), observation(1, step, {"b": 2}, filename=second)]
                groups = list(closed_groups(source, final=True))
                self.assertEqual(len(groups), expected)
                if step == 0:
                    self.assertEqual(visible_values(groups[0]), {"a": 1, "b": 2, "global_step": 0, "_timestamp": 101.})
                else:
                    self.assertEqual(groups[0]["next"]["origin"]["filename"], second)
                    self.assertEqual(groups[1]["prefix"][0]["origin"]["filename"], second)

    def test_strict_group_revisions_source_order_and_int64_steps(self):
        source = [observation(0, 0), observation(1, 2**53, {"a": 1}),
                  observation(2, 2**53 + 1, {"a": 2}), observation(3, -(2**63), {"a": 3})]
        groups = list(closed_groups(source, final=True))
        self.assertEqual([history_metrics(group)["global_step"] for group in groups], [2**53, 2**53 + 1, -(2**63)])
        group = groups[0]
        metrics = history_metrics(group)
        self.assertEqual(validate_group_origin(metrics["_tensorboard_record"], metrics["_tensorboard_origin"], group), group["revision"])
        malformed = []
        bad = copy.deepcopy(group); bad["revision"] = "0" * 64; malformed.append(bad)
        bad = copy.deepcopy(group); bad["version"] = True; malformed.append(bad)
        bad = copy.deepcopy(group); bad["prefix"][0]["metrics"] = {"not-empty": 1}; malformed.append(bad)
        bad = copy.deepcopy(group); bad["members"][0]["index"] = 2; malformed.append(bad)
        bad = copy.deepcopy(group); bad["members"][0]["step"] = "01"; malformed.append(bad)
        bad = copy.deepcopy(group); bad["members"][0]["step"] = str(2**63); malformed.append(bad)
        bad = copy.deepcopy(group); bad["members"][0]["wall_time"] = float("nan"); malformed.append(bad)
        bad = copy.deepcopy(group); bad["next"]["metrics"] = {}; malformed.append(bad)
        for bad in malformed:
            with self.subTest(group=bad), self.assertRaises(ValueError):
                validate_group(bad)

    def test_local_source_snapshot_isolated_from_writer_append(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            original = root / "events.out.tfevents.1.a"
            original.write_bytes(b"first-source-prefix")
            run = type("SnapshotOwner", (), {"sync_dir": str(Path(directory) / "journal")})()
            local, cache = sources.snapshot(run, str(original), str(root))
            original.write_bytes(b"first-source-prefix-late-append")
            self.assertEqual(Path(local).read_bytes(), b"first-source-prefix")
            updated, updated_cache = sources.snapshot(run, str(original), str(root))
            self.assertEqual((local, cache), (updated, updated_cache))
            self.assertEqual(Path(updated).read_bytes(), original.read_bytes())

    def test_canonical_replay_and_summary_for_independent_source_views(self):
        """Check complete storage boundaries against manually expected results.

        Main callers: portable optional-test discovery. Physical events remain
        immutable across reverse or duplicated delivery. Source-order groups
        revise one logical sample, while explicit edits and ordinary samples
        retain their own summary effects.
        """
        from backprop.apis.public.replay import reduce_events
        from backprop.tensorboard_replay import semantic_value
        from tensorboard_grouping_replay_fixture import fixtures
        output = ROOT / "test-results/tensorboard-grouping-replay.json"
        with patch.dict(sys.modules, {"PIL": None}):
            cases = fixtures(output)["cases"]
        self.assertEqual(len(cases), 17)
        for name, case in cases.items():
            original = copy.deepcopy(case["events"])
            for delivery, events in (("original", case["events"]),
                                     ("reverse", list(reversed(case["events"]))),
                                     ("duplicates", case["events"] * 2)):
                with self.subTest(case=name, delivery=delivery):
                    if case["expect_conflict"]:
                        with self.assertRaises(ValueError):
                            reduce_events(events)
                        continue
                    state = reduce_events(events)
                    rows = [{key: semantic_value(value) for key, value in row.items()
                             if not key.startswith("_") or key in {"_timestamp", "_tensorboard_incomplete"}}
                            for row in state["history"]]
                    summary = {key: semantic_value(value) for key, value in state["summary"].items()
                               if not key.startswith("_") or key == "_timestamp"}
                    self.assertEqual(rows, case["expected_history"])
                    self.assertEqual(summary, case["expected_summary"])
                    self.assertEqual(len(state["writers"]), case["expected_writer_count"])
                    self.assertEqual({writer["state"] for writer in state["writers"].values()}, {"finished"})
            self.assertEqual(case["events"], original)

    def test_same_imported_chart_repeated_encoding_has_one_deterministic_manifest(self):
        observations = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
            for label in ("a", "b"):
                table = backprop.Table(columns=["recall", "precision"],
                    data=[[index / 6001, (6000 - index) / 6001] for index in range(6000)])
                chart = backprop.plot_table("wandb/line/v0", table,
                    {"x": "recall", "y": "precision"}, {"title": "curve Precision v. Recall"})
                source = [observation(0, 0), observation(1, 8, {"curve": chart})]
                stabilize_table_ids(source[1]["metrics"], record_identity_from_origin(source[1]["origin"]))
                group = list(closed_groups(source, final=True))[0]
                sink = backprop.MemorySink()
                with backprop.Run(id="grouped-pr", entity="e", project="tb-gap", mode="shared",
                    dir=directory, _sink=sink, _deployment={}, settings={"silent": True, "console": "off",
                        "disable_git": True, "x_disable_stats": True, "x_label": label, "x_primary": False}) as run:
                    run.log(history_metrics(group))
                payload = next(event["payload"] for event in sink.events if event["type"] == "history")
                self.assertEqual(len(payload["artifacts"]), 1)
                top = payload["metrics"]["curve"]["table"]
                member = payload["metrics"]["_tensorboard_group"]["members"][0]["metrics"]["curve"]["table"]
                self.assertTrue(top["external"])
                self.assertEqual(top["sha256"], member["sha256"])
                encoded = sink.download_file(top)
                self.assertEqual(encoded, sink.download_file(member))
                self.assertEqual(hashlib.sha256(encoded).hexdigest(), top["sha256"])
                observations.append((encoded, payload["artifacts"][0]["artifact_id"]))
            self.assertEqual(observations[0], observations[1])


@unittest.skipUnless(HAS_TB and HAS_MEDIA, "optional tensorboard/Pillow/NumPy are not installed")
class TensorBoardGroupingRealContracts(unittest.TestCase):
    def test_real_live_tail_restart_same_step_append_retry_and_copied_source(self):
        """Exercise genuine source appends across independent durable handles.

        Main caller: optional CI. Empty live tails are not volatile state: the
        next process rebuilds them from frozen source bytes. Final historical
        groups accept same-step extension without acquiring another visible row.
        """
        import shutil
        from tensorboard.summary.writer.event_file_writer import EventFileWriter
        from tensorboard.compat.proto.event_pb2 import Event
        from tensorboard.compat.proto.summary_pb2 import Summary
        from backprop.apis.public import Api
        from backprop.integration import tensorboard as tb
        tb.unpatch()
        settings = {"silent": True, "console": "off", "disable_git": True,
                    "x_disable_stats": True, "flush_interval": 3600}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
            root = Path(directory) / "source"
            writer = EventFileWriter(str(root))
            sink = backprop.MemorySink()
            handles = []
            def run_for(label):
                with patch.dict(os.environ, {"BACKPROP_DIR": str(Path(directory) / label)}):
                    run = backprop.Run(id="append", entity="e", project="tb-group", dir=directory,
                        resume="allow", _sink=sink, _deployment={}, settings=settings)
                handles.append(run)
                return run
            def append(step, wall_time, **metrics):
                writer.add_event(Event(step=step, wall_time=wall_time, summary=Summary(value=[
                    Summary.Value(tag=key, simple_value=value) for key, value in metrics.items()])))
                writer.flush()
            def rows(run):
                run.flush()
                return list(Api._from_store(sink, entity="e", project="tb-group").run(run.id).scan_history())
            try:
                first = run_for("first")
                append(5, 100., loss=1)
                self.assertEqual(tb.sync(first, root, final=False), 0)
                append(5, 101., sparse=2)
                append(5, 102., loss=3)
                self.assertEqual(tb.sync(first, root, final=False), 0)
                self.assertEqual(rows(first), [])
                first.finish()
                restarted = run_for("restarted")
                self.assertEqual(tb.sync(restarted, root, final=False), 0)
                append(6, 103., next=4)
                self.assertEqual(tb.sync(restarted, root, final=False), 1)
                self.assertEqual([(row["loss"], row["sparse"]) for row in rows(restarted)], [(3, 2)])
                self.assertEqual(tb.sync(restarted, root, final=True), 1)
                before = rows(restarted)
                append(6, 104., next=8)
                self.assertEqual(tb.sync(restarted, root, final=True), 1)
                after = rows(restarted)
                self.assertEqual(len(after), 2)
                self.assertEqual(after[1]["next"], 8)
                self.assertEqual(before[1]["_tensorboard_record"], after[1]["_tensorboard_record"])
                append(6, 105., retry=9)
                with patch.object(restarted, "log", side_effect=RuntimeError("journal append failed")):
                    with self.assertRaisesRegex(RuntimeError, "journal append failed"):
                        tb.sync(restarted, root, final=True)
                self.assertEqual(tb.sync(restarted, root, final=True), 1)
                self.assertEqual(rows(restarted)[1]["retry"], 9)
                writer.close()
                restarted.finish()
                copied = Path(directory) / "copied"
                shutil.copytree(root, copied)
                recovered = run_for("recovered")
                self.assertEqual(tb.sync(recovered, copied, final=True), 0)
                final_rows = rows(recovered)
                self.assertEqual([row["global_step"] for row in final_rows], [5, 6])
                saved = [event["payload"] for event in sink.events if event["type"] == "file"
                         and "tfevents" in event["payload"]["name"]]
                self.assertEqual(sink.download_file(saved[-1]["file"]), next(root.glob("*tfevents*")).read_bytes())
            finally:
                writer.close()
                for run in handles:
                    run.finish()
                tb.unpatch()

    def test_pinned_same_step_groups_repeated_tags_file_boundaries_and_timestamps(self):
        output = ROOT / "test-results/tensorboard-grouping-runtime.json"
        output.parent.mkdir(exist_ok=True)
        observations = {}
        for mode in ("reference", "native"):
            env = {**os.environ, "WANDB_DATA_DIR": "/tmp/backprop-reference-data",
                   "WANDB_CACHE_DIR": "/tmp/backprop-reference-cache",
                   "PYTHONPATH": str(ROOT / "sdk") if mode == "native" else "",
                   "BACKPROP_TENSORBOARD_GROUPING_OUTPUT": str(output.with_name("tensorboard-grouping-" + mode + "-raw.json"))}
            result = subprocess.run([sys.executable, str(ROOT / "tests/tensorboard_grouping_probe.py"), mode],
                                    env=env, capture_output=True, text=True, timeout=180)
            output.with_name("tensorboard-grouping-" + mode + "-process.json").write_text(
                json.dumps({"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}, indent=2) + "\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            observations[mode] = json.loads(result.stdout)
            output.write_text(json.dumps(observations, indent=2) + "\n")
        self.maxDiff = None
        reference, native = observations["reference"], observations["native"]
        self.assertEqual(reference["single_stream"], native["single_stream"])
        self.assertEqual({name: len(rows) for name, rows in native["single_stream"].items()},
            {"same_file": 4, "files_nonzero": 2, "files_zero": 1, "zero_file_barrier": 3,
             "metadata_omitted": 2, "rich_last_values": 1})
        self.assertEqual(native["single_stream"]["same_file"][0],
            {"loss": 2., "accuracy": .75, "global_step": 5, "_timestamp": 103.})
        self.assertEqual(native["single_stream"]["zero_file_barrier"][0]["_timestamp"], 2_000_000_031.)
        for observed in (reference, native):
            self.assertEqual({name: count for name, count in observed["watch_counts"].items()
                if name != "repeated_discovery"}, {name: 1 for name in native["single_stream"]})
        # W&B starts two independent streams when patched constructors discover
        # the same directory twice. Foundry intentionally deduplicates identical
        # source records. Preserve that real multiplicity difference explicitly;
        # no reference rows are filtered to manufacture equality.
        self.assertEqual(reference["watch_counts"]["repeated_discovery"], 2)
        self.assertEqual(native["watch_counts"]["repeated_discovery"], 1)
        expected = [{"a": 1., "global_step": 5, "_timestamp": 100.},
                    {"b": 2., "global_step": 5, "_timestamp": 110.}]
        self.assertEqual(native["repeated_discovery"], expected)
        self.assertEqual(len(reference["repeated_discovery"]), 4)
        for row in expected:
            self.assertEqual(reference["repeated_discovery"].count(row), 2)


if __name__ == "__main__":
    unittest.main()
