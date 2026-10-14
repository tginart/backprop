"""Compare genuine offline W&B TensorBoard ingestion with native histories.

Architecture: identical real EventFileWriter protobuf fixtures -> patched SDK
writers and run finish -> reference service teardown -> immutable durable log
-> normalized scalar/histogram/image/PR observations. Main caller: optional CI.
There is no W&B service fixture in this probe: pinned W&B runs fully offline.
The small IEEE LevelDB reader follows the pinned transactionlog/leveldb source
and validates checksums, so reading local Go-core output needs no extra package.
Raw transaction records, final core responses and local files are saved before
history paths are reconstructed or selected values are asserted. This preserves
actual observations even when a later contract comparison fails.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from tensorboard_gap_fixtures import write, extended_events, unsupported_events, integer_events
from tensorboard_gap_observations import history_row
from tensorboard_gap_transaction import completed_reference_records

REFERENCE = sys.argv[1] == "reference"
EXTENDED = len(sys.argv) > 2 and sys.argv[2] == "extended-media"
INTEGER = len(sys.argv) > 2 and sys.argv[2] == "int64-boundaries"
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == "0.30.0"
else:
    import backprop as sdk
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def pixels(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGBA")
        return {"size": list(image.size), "pixels": hashlib.sha256(image.tobytes()).hexdigest()}


def extended_output(latest, rows, files_dir, sink):
    """Compare encoded media bytes and declared dimensions across SDKs.

    Main caller: the additional supported-plugin runtime probe. Reading the
    metadata independently of the image bytes detects a missing JPEG size;
    hashing the full encoded file detects changes to GIF animation frames.
    """
    output = {"histograms": {tag: latest[tag] for tag in ("empty_histogram", "nonempty_histogram")},
              "global_steps": [row["global_step"] for row in rows],
              "wall_times": [row["_timestamp"] for row in rows], "images": {}}
    for tag in ("legacy_png", "jpeg_frames", "gif_frames"):
        value = latest[tag]
        if REFERENCE:
            encoded = [(files_dir / filename).read_bytes() for filename in value["filenames"]]
            dimensions = [[value["width"], value["height"]]] * value["count"]
            count = value["count"]
        else:
            encoded = [sink.download_file(item) for item in value]
            dimensions = [[item["width"], item["height"]] for item in value]
            count = len(value)
        if count != len(encoded):
            raise ValueError("TensorBoard image count disagrees with its saved files")
        output["images"][tag] = {"count": count, "dimensions": dimensions,
            "encoded_sha256": [hashlib.sha256(data).hexdigest() for data in encoded],
            "pixels": [pixels(data) for data in encoded]}
    return output


def preserve_raw(run, records, sink, logdir, files_dir, transaction_bytes=None):
    """Save actual SDK evidence before interpreting any history values.

    Main caller: the genuine-writer comparison after reference teardown. Why this
    exists: a missing or mismatched upstream plugin must leave inspectable
    transaction records and core responses, rather than just a KeyError. The
    reference transaction log and source event bytes remain verbatim; native
    sink events are recorded before resolving portable media references.
    """
    output = Path(os.environ.get("BACKPROP_TENSORBOARD_PROBE_OUTPUT",
        str(Path(__file__).resolve().parents[1] / "test-results" /
            ("tensorboard-gap-runtime-" + ("reference" if REFERENCE else "native") + "-raw.json"))))
    output.parent.mkdir(parents=True, exist_ok=True)
    source_files = [{"name": path.name, "bytes_hex": path.read_bytes().hex()}
                    for path in sorted(Path(logdir).glob("*tfevents*"))]
    local_files = [{"name": str(path.relative_to(files_dir)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                   for path in sorted(files_dir.rglob("*")) if path.is_file()]
    raw = {"version": sdk.__version__, "source_files": source_files, "files": local_files}
    if REFERENCE:
        from google.protobuf.json_format import MessageToDict
        raw.update({"transaction_records": [MessageToDict(record, preserving_proto_field_name=True)
                                             for record in records],
                    "transaction_capture_boundary": "wandb.teardown",
                    "transaction_sha256": hashlib.sha256(transaction_bytes).hexdigest(),
                    "transaction_size": len(transaction_bytes),
                    "final_summary": MessageToDict(run._final_summary, preserving_proto_field_name=True),
                    "sampled_history": MessageToDict(run._sampled_history, preserving_proto_field_name=True),
                    "core_log": Path(run._settings.log_internal).read_text()})
        output.with_suffix(".wandb").write_bytes(transaction_bytes)
    else:
        raw["sink_events"] = sink.events
    output.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")


def unsupported_boundary_output():
    """Observe genuine writer output before interpreting upstream omissions.

    Main caller: the optional graph/plugin boundary comparison. Each source has
    its own real run and event file. Reference omission and native visible
    errors are distinct observations; raw records and process evidence survive
    any later assertion. Native lifecycle cleanup is also checked by callers.
    """
    observations = {}
    evidence = os.environ.get("BACKPROP_TENSORBOARD_PROBE_OUTPUT")
    for name, event in unsupported_events().items():
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
            logdir = str(Path(directory) / "events")
            sdk.tensorboard.patch(save=True, root_logdir=logdir)
            sink = None if REFERENCE else sdk.MemorySink()
            run = sdk.init(id="tb-boundary-" + name, entity="e", project="tb-gap", dir=directory,
                mode="offline" if REFERENCE else "online", sync_tensorboard=True,
                settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True},
                **({} if REFERENCE else {"_sink": sink}))
            error = None
            try:
                write(logdir, [event])
                try:
                    run.finish()
                except Exception as failure:
                    error = type(failure).__name__
                records, transaction_bytes = completed_reference_records(run) if REFERENCE else ([], None)
                destination = str(Path(evidence).with_name(Path(evidence).stem + "-" + name + ".json")) if evidence else ""
                with patch.dict(os.environ, {"BACKPROP_TENSORBOARD_PROBE_OUTPUT": destination} if evidence else {}):
                    preserve_raw(run, records, sink, logdir, Path(run.dir), transaction_bytes)
                rows = ([history_row(record.history.item) for record in records
                         if record.WhichOneof("record_type") == "history"] if REFERENCE else
                        [event["payload"]["metrics"] for event in sink.events if event["type"] == "history"])
                metrics = {key: value for row in rows for key, value in row.items()
                           if not key.startswith("_") and key != "global_step"}
                observations[name] = {"error": error, "metrics": metrics}
            finally:
                sdk.tensorboard.unpatch()
    return observations


if len(sys.argv) > 2 and sys.argv[2] == "history-path-readback":
    if not REFERENCE:
        raise ValueError("History readback validates only the pinned W&B proto")
    from wandb.proto.wandb_internal_pb2 import HistoryItem
    # Exact field paths observed from the real pinned offline Go core. This
    # focused mode needs neither a TensorBoard install nor a local-core socket.
    items = [HistoryItem(nested_key=["legacy_weights", "bins"], value_json="[0,1,2]"),
             HistoryItem(nested_key=["legacy_weights", "values"], value_json="[2,3]"),
             HistoryItem(nested_key=["legacy_weights", "_type"], value_json='"histogram"'),
             HistoryItem(nested_key=["train/loss"], value_json="0.25"),
             HistoryItem(nested_key=["train/frame", "filenames"], value_json='["media/images/frame.png"]'),
             HistoryItem(key="global_step", value_json="8")]
    print(json.dumps(history_row(items), sort_keys=True))
    sys.exit(0)

if len(sys.argv) > 2 and sys.argv[2] == "transaction-readback":
    if not REFERENCE:
        raise ValueError("Transaction readback validates only the pinned W&B log")
    with tempfile.TemporaryDirectory() as directory:
        run = sdk.init(id="tb-log-parser", project="tb-gap", dir=directory, mode="offline",
                       settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True})
        run.log({"parser_value": 123})
        run.finish()
        records, transaction_bytes = completed_reference_records(run)
        preserve_raw(run, records, None, directory, Path(run.dir), transaction_bytes)
        data = [json.loads(item.value_json) for record in records
                if record.WhichOneof("record_type") == "history"
                for item in record.history.item if (item.key or "/".join(item.nested_key)) == "parser_value"]
        print(json.dumps({"parser_values": data}))
    sys.exit(0)

if len(sys.argv) > 2 and sys.argv[2] == "unsupported-boundaries":
    print(json.dumps(unsupported_boundary_output(), sort_keys=True))
    sys.exit(0)

with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
    logdir = str(Path(directory) / "events")
    sdk.tensorboard.patch(save=True, root_logdir=logdir)
    sink = None if REFERENCE else sdk.MemorySink()
    options = {} if REFERENCE else {"_sink": sink}
    run = sdk.init(id="tb-runtime", entity="e", project="tb-gap", dir=directory,
                   mode="offline" if REFERENCE else "online", sync_tensorboard=True,
                   settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True},
                   **options)
    files_dir = Path(run.dir)
    try:
        write(logdir, integer_events() if INTEGER else extended_events() if EXTENDED else None)
        run.finish()
        records, transaction_bytes = completed_reference_records(run) if REFERENCE else ([], None)
        preserve_raw(run, records, sink, logdir, files_dir, transaction_bytes)
        if REFERENCE:
            rows = [history_row(record.history.item) for record in records
                    if record.WhichOneof("record_type") == "history"]
        else:
            rows = [event["payload"]["metrics"] for event in sink.events if event["type"] == "history"]
        latest = {}
        for row in rows:
            latest.update(row)
        if INTEGER:
            output = {"scalars": [latest["int64_" + str(index)] for index in range(6)],
                      "global_steps": [row["global_step"] for row in rows],
                      "wall_times": [row["_timestamp"] for row in rows]}
        elif EXTENDED:
            output = extended_output(latest, rows, files_dir, sink)
        else:
            output = {"losses": [row["loss"] for row in rows if "loss" in row],
                      "weights": [row["weights"] for row in rows if "weights" in row],
                      "legacy_weights": latest["legacy_weights"],
                      "global_steps": [row["global_step"] for row in rows if "loss" in row],
                      "wall_times": [row["_timestamp"] for row in rows if "loss" in row]}
            if REFERENCE:
                output["image"] = pixels((files_dir / latest["frame"]["filenames"][0]).read_bytes())
                table = json.loads((files_dir / latest["curve"]["path"]).read_text())
            else:
                resolved = [run._journal._resolve(event) for event in __import__("backprop.transport", fromlist=["read_journal"]).read_journal(run.sync_dir)
                            if event["type"] == "history"]
                first = resolved[0]["payload"]["metrics"]
                output["image"] = pixels(sink.download_file(first["frame"][0]))
                table = first["curve"]["table"]
            output["pr_curve"] = {"columns": table["columns"], "data": table["data"]}
        output["source_file_preserved"] = any("tfevents" in str(path) for path in files_dir.rglob("*")) if REFERENCE else any(
            event["type"] == "file" and "tfevents" in event["payload"]["name"] for event in sink.events)
    finally:
        sdk.tensorboard.unpatch()
print(json.dumps(output, sort_keys=True))
