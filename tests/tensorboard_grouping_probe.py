"""Genuine writers -> pinned offline core/native importer -> grouped histories.

Architecture: fixed writer clock and real protobuf events -> ordinary patched
EventFileWriter files -> run finish/service teardown -> immutable raw evidence
-> source-value comparisons. Main caller: optional grouping CI. Only the writer
clock is fixed so file-header wall times agree across isolated SDK processes.
No service responses, event parser or media conversion are replaced.
Single-stream rollover files use the captured original writer constructor;
repeated patched-writer discovery is measured separately without deduplicating
its actual reference rows.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from tensorboard_gap_fixtures import png, encoded_image, tensor_value
from tensorboard_gap_observations import history_row
from tensorboard_gap_transaction import completed_reference_records

REFERENCE = sys.argv[1] == "reference"
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == "0.30.0"
else:
    import backprop as sdk
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def source_cases():
    """Keep expected grouping triggers in genuine protobuf source order.

    Main caller: compare. Repeated tags and empty summaries exercise timestamp
    overwrites. Actual writer file-version events test step-zero file boundaries.
    Repeated PR/image tags ensure overwritten rich values remain source evidence.
    """
    import numpy as np
    from tensorboard.compat.proto.event_pb2 import Event
    from tensorboard.compat.proto.summary_pb2 import Summary

    def event(step, time, **metrics):
        return Event(step=step, wall_time=time, summary=Summary(value=[
            Summary.Value(tag=key, simple_value=value) for key, value in metrics.items()]))

    curve = lambda tag, recall: tensor_value(tag, np.array([[0., 0.]] * 4 + [[.75, .5], recall]), "pr_curves")
    rich = [Event(step=5, wall_time=100., summary=Summary(value=[
        tensor_value("frame", [b"2", b"3", png()], "images"), curve("curve", [.25, .75]),
        tensor_value("weights", [[0., 1., 2.], [1., 2., 3.]], "histograms")])),
        Event(step=5, wall_time=101., summary=Summary(value=[
            tensor_value("frame", [b"2", b"3", encoded_image("JPEG")], ""),
            curve("curve", [.5, .8]), tensor_value("weights", [[0., 1., 4.], [1., 2., 1.]], "")]))]
    cases = {
        "same_file": [[event(5, 100., loss=1), event(5, 101., accuracy=.75), event(5, 102., loss=2),
                       event(5, 103.), event(3, 104., loss=4), event(3, 105., sparse=.5),
                       event(8, 106., loss=8), event(5, 107., loss=5)]],
        "files_nonzero": [[event(5, 100., a=1)], [event(5, 110., b=2)]],
        "files_zero": [[event(0, 100., a=1)], [event(0, 110., b=2)]],
        "zero_file_barrier": [[event(0, 100., a=1)], [event(1, 110., middle=2), event(0, 120., c=3)]],
        "metadata_omitted": [[Event(step=2, wall_time=100., summary=Summary(value=[tensor_value("named", .25, "scalars")])),
                              Event(step=2, wall_time=101., summary=Summary(value=[tensor_value("named", .5, "")])),
                              event(3, 102., after=1)]],
        "rich_last_values": [rich],
    }
    cases["repeated_discovery"] = cases["files_nonzero"]
    return cases


def normalize(rows, files_dir, sink):
    """Compare source metrics, exact timestamps and immutable rich bytes.

    Main caller: compare after raw observations are saved. Placement paths vary
    across SDKs; image bytes and PR-table data are read from their actual outputs.
    """
    output = []
    for row in rows:
        observed = {key: value for key, value in row.items()
                    if not key.startswith("_") or key == "_timestamp"}
        if "frame" in observed:
            image = observed["frame"]
            data = ((files_dir / image["filenames"][0]).read_bytes() if REFERENCE else sink.download_file(image[0]))
            observed["frame"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        if "curve" in observed:
            table = (json.loads((files_dir / observed["curve"]["path"]).read_text()) if REFERENCE else observed["curve"]["table"])
            observed["curve"] = {"columns": table["columns"], "data": table["data"]}
        output.append(observed)
    return output


def compare():
    """Run independent genuine offline comparisons, retaining raw failures.

    Main caller: the isolated CLI. All files and actual records are preserved
    before public replay or selected-metric normalization can fail. Public
    native readback exercises the shared group projection as well as importing.
    """
    from tensorboard.summary.writer import event_file_writer
    observations = {}
    watch_counts = {}
    raw_output = Path(os.environ.get("BACKPROP_TENSORBOARD_GROUPING_OUTPUT", "test-results/tensorboard-grouping-raw.json"))
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    raw_cases = {}
    for case_index, (name, files) in enumerate(source_cases().items()):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
            logdir = Path(directory) / "events"
            logdir.mkdir()
            original_writer = event_file_writer.EventFileWriter
            original_init = original_writer.__init__
            sdk.tensorboard.patch(save=True, root_logdir=str(logdir))
            sink = None if REFERENCE else sdk.MemorySink()
            run = sdk.init(id="tb-group-" + name, entity="e", project="tb-group", dir=directory,
                mode="offline" if REFERENCE else "online", sync_tensorboard=True,
                settings={"silent": True, "console": "off", "disable_git": True, "x_disable_stats": True},
                **({} if REFERENCE else {"_sink": sink, "_deployment": {}}))
            try:
                for file_index, events in enumerate(files):
                    with patch.object(event_file_writer.time, "time", return_value=float(2_000_000_000 + case_index * 10 + file_index)):
                        if file_index == 0 or name == "repeated_discovery":
                            writer = event_file_writer.EventFileWriter(str(logdir))
                        else:
                            # Native hooks mutate __init__ in place; upstream
                            # replaces the module's class. Calling the captured
                            # original constructor handles both mechanisms.
                            # These are real rollover files for one watched
                            # stream, not another directory-discovery callback.
                            writer = original_writer.__new__(original_writer)
                            original_init(writer, str(logdir))
                    for event in events:
                        writer.add_event(event)
                    writer.flush()
                    writer.close()
                run.finish()
                evidence = {"source_files": [{"name": path.name, "bytes_hex": path.read_bytes().hex()}
                    for path in sorted(logdir.glob("*tfevents*"))]}
                if REFERENCE:
                    from google.protobuf.json_format import MessageToDict
                    records, data = completed_reference_records(run)
                    evidence.update({"transaction_bytes_hex": data.hex(),
                        "transaction_records": [MessageToDict(record, preserving_proto_field_name=True) for record in records],
                        "final_summary": MessageToDict(run._final_summary, preserving_proto_field_name=True),
                        "core_log": Path(run._settings.log_internal).read_text()})
                    watch_counts[name] = sum("tensorboard: tracking new log directory" in line
                        for line in evidence["core_log"].splitlines())
                else:
                    evidence["sink_events"] = sink.events
                    evidence["sink_files"] = {key: data.hex() for key, data in sink.files.items()}
                    watch_counts[name] = len(sdk.tensorboard.directories(run))
                evidence["watch_count"] = watch_counts[name]
                raw_cases[name] = evidence
                raw_output.write_text(json.dumps({"version": sdk.__version__, "cases": raw_cases}, indent=2) + "\n")
                if REFERENCE:
                    rows = [history_row(record.history.item) for record in records if record.WhichOneof("record_type") == "history"]
                else:
                    from backprop.apis.public import Api
                    rows = list(Api._from_store(sink, entity="e", project="tb-group").run(run.id).scan_history())
                observations[name] = normalize(rows, Path(run.dir), sink)
            finally:
                sdk.tensorboard.unpatch()
    return {"single_stream": {key: value for key, value in observations.items() if key != "repeated_discovery"},
            "repeated_discovery": observations["repeated_discovery"], "watch_counts": watch_counts}


print(json.dumps(compare(), sort_keys=True))
