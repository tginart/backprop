"""Observe real writer tails after unpatch with isolated pinned/native SDKs.

Architecture: actual optional writer -> live global/nonglobal runs -> unpatch
and further writes -> finished core/journal capture -> native durable retry and
restart. Main caller: test_optional_tensorboard_gap_unpatch. Why this exists:
constructor restoration must not revoke a run's already announced source.
"""
import base64
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

from tensorboard_gap_observations import history_row
from tensorboard_gap_transaction import completed_reference_records

REFERENCE = sys.argv[1] == "reference"
FAMILY, ALIAS = sys.argv[2:4]
OUTPUT = Path(sys.argv[-1])
if REFERENCE:
    import wandb as sdk
    assert sdk.__version__ == "0.30.0"
else:
    import backprop as sdk

ALIASES = ("wandb.tensorboard", "wandb.integration.tensorboard")
root_import = None
if FAMILY == "alias-preflight":
    try:
        importlib.import_module(sdk.__name__ + ".tensorboard")
    except ModuleNotFoundError as error:
        root_import = {"error": type(error).__name__, "name": error.name}
# The root alias is a public SDK attribute. Pinned W&B0.30.0 supplies it through
# a LazyLoader and its module path cannot be imported before that lazy attribute
# loads it. Access the real attribute separately from the canonical module.
public_modules = {ALIASES[0]: sdk.tensorboard,
                  ALIASES[1]: importlib.import_module(sdk.__name__ + ".integration.tensorboard")}
canonical = public_modules[ALIASES[1]]
aliases = {name: module.patch is canonical.patch and module.unpatch is canonical.unpatch
           for name, module in public_modules.items()}
if FAMILY == "alias-preflight":
    empty_unpatch = {name: module.unpatch() for name, module in public_modules.items()}
    OUTPUT.write_text(json.dumps({"sdk": sdk.__version__, "aliases": aliases,
        "access": {ALIASES[0]: sdk.__name__ + ".tensorboard (attribute)",
                   ALIASES[1]: sdk.__name__ + ".integration.tensorboard (module)"},
        "signatures": {name: {method: str(inspect.signature(getattr(module, method)))
                              for method in ("patch", "unpatch")}
                       for name, module in public_modules.items()},
        "empty_unpatch": empty_unpatch, "root_module_import": root_import,
        "root_module_import_phase": "Before accessing the public lazy attribute"},
        indent=2, sort_keys=True) + "\n")
    raise SystemExit(0)

SETTINGS = {"silent": True, "console": "off", "x_disable_stats": True,
            "disable_git": True, "disable_code": True}
if not REFERENCE:
    # Native polling is controlled explicitly by this probe. W&B has no
    # flush_interval setting, and its real core watcher keeps its own schedule.
    SETTINGS["flush_interval"] = 3600
validated_settings = sdk.Settings(**SETTINGS)
writer_module = None
if FAMILY != "lifecycle-preflight":
    MODULE = {"event": "tensorboard.summary.writer.event_file_writer",
              "torch": "torch.utils.tensorboard.writer", "tensorboardx": "tensorboardX.writer"}[FAMILY]
    writer_module = importlib.import_module(MODULE)


def writer(directory, *, filename_suffix=""):
    """Let the installed writer name and encode its own independent stream.

    Main callers: initial, resumed and destructive-collision writer fixtures.
    Why this exists: TensorBoardX names files by second and host without a
    process/counter suffix, so a new writer needs its documented filename_suffix
    to retain the original append-only source during this restart comparison.
    """
    if FAMILY == "event":
        return writer_module.EventFileWriter(str(directory), filename_suffix=filename_suffix)
    return writer_module.SummaryWriter(str(directory), filename_suffix=filename_suffix)


def emit(output, step, metrics):
    """Write genuine scalar events with exact comparable source wall times.

    Main callers: initial stream, post-unpatch append and native resumed stream.
    Framework scalar helpers retain their own serialization; direct TensorBoard
    uses its actual protobufs. Same-step overwrites exercise the real grouper.
    """
    if FAMILY == "event":
        from tensorboard.compat.proto.event_pb2 import Event
        from tensorboard.compat.proto.summary_pb2 import Summary
        output.add_event(Event(step=step, wall_time=100. + step,
            summary=Summary(value=[Summary.Value(tag=key, simple_value=value) for key, value in metrics.items()])))
    else:
        for key, value in metrics.items():
            output.add_scalar(key, value, global_step=step, walltime=100. + step)
    output.flush()


def native_events(run):
    from backprop.transport import read_journal
    return read_journal(run.sync_dir)


def rows(run):
    """Capture finished bytes before decoding, retaining physical evidence.

    Main callers: both SDK runs after finish. Native canonical projection must
    reconcile revised source groups while every journal event remains in raw
    evidence; source rows retain their exact core transaction multiplicity.
    """
    if REFERENCE:
        from google.protobuf.json_format import MessageToDict
        records, frozen = completed_reference_records(run)
        OUTPUT.with_name(OUTPUT.stem + "-" + run.id + ".wandb").write_bytes(frozen)
        physical = [MessageToDict(record) for record in records]
        return ([history_row(record.history.item) for record in records
                 if record.WhichOneof("record_type") == "history"],
                {"transaction_sha256": hashlib.sha256(frozen).hexdigest(),
                 "transaction_size": len(frozen), "transaction_records": physical})
    from backprop.apis.public.replay import reduce_events
    events = native_events(run)
    return reduce_events(events)["history"], {"physical_events": events}


def selected(rows):
    """Keep all TensorBoard row multiplicity and the complete selected tags."""
    return [{key: value for key, value in row.items() if key == "global_step" or key.startswith("train/")}
            for row in rows if "global_step" in row or any(key.startswith("train/") for key in row)]


def source_objects(root):
    """Retain exact emitted bytes and independently decoded source records."""
    from tensorboard.backend.event_processing.event_file_loader import RawEventFileLoader
    from tensorboard.compat.proto.event_pb2 import Event
    result = []
    for path in sorted(root.rglob("*tfevents*")):
        frozen = path.read_bytes()
        records = []
        for raw in RawEventFileLoader(str(path)).Load():
            event = Event.FromString(raw)
            records.append({"step": event.step, "wall_time": event.wall_time,
                            "field": event.WhichOneof("what"),
                            "metrics": {item.tag: item.simple_value for item in event.summary.value
                                        if item.HasField("simple_value")}})
        result.append({"name": str(path.relative_to(root)), "bytes": len(frozen),
                       "sha256": hashlib.sha256(frozen).hexdigest(),
                       "base64": base64.b64encode(frozen).decode(), "records": records})
    return result


def restart(directory, root, sink, public):
    """Retry one failed source read, then finish and recover canonical history.

    Main caller: native initial capture. The real reader/journal are exercised;
    only a single source-open failure is injected. Durable identities and
    release ownership must survive retry, resume and another patch session.
    """
    from backprop.integration.tensorboard import reader
    original_sources = source_objects(root)
    public.patch(save=False, root_logdir=str(root))
    # Pinned and native offline init ignore resume. Native's real Run with its
    # existing MemorySink restores the durable journal without hosted I/O.
    recovered = sdk.Run(id="owner", project="tb-unpatch", entity="e", dir=directory,
        mode="online", settings=SETTINGS, _sink=sink, resume="must")
    current = writer(root / "train", filename_suffix=".restart")
    emit(current, 4, {"loss": 1.25})
    public.unpatch()
    current.close()
    before = native_events(recovered)
    restart_sources = source_objects(root)
    original_hashes = {source["name"]: source["sha256"] for source in original_sources}
    restart_hashes = {source["name"]: source["sha256"] for source in restart_sources}
    new_sources = sorted(set(restart_hashes) - set(original_hashes))
    preserved = all(restart_hashes.get(name) == digest for name, digest in original_hashes.items())
    checkpoint = {"original_source_hashes": original_hashes, "original_sources_unchanged": preserved,
                  "new_source_names": new_sources, "source_objects": restart_sources,
                  "physical_events_before_read": before}
    # Preserve actual bytes before any reader/replay failure can abort the probe.
    OUTPUT.with_name(OUTPUT.stem + "-restart-before-read.json").write_text(
        json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
    assert preserved, "Restart writer changed an original source file"
    assert len(new_sources) == 1 and new_sources[0].startswith("train/") and new_sources[0].endswith(".restart"), new_sources
    failure = None
    injected = patch.object(reader, "snapshot", side_effect=OSError("selected source temporarily unavailable"))
    with injected as failed_snapshot:
        try:
            recovered.flush()
        except OSError as error:
            failure = type(error).__name__
    injected_calls = failed_snapshot.call_count
    unchanged = native_events(recovered) == before
    recovered.flush()
    recovered.finish()
    history, raw = rows(recovered)
    from backprop.integration import tensorboard as tb
    result = {"read_failure": failure, "injected_read_calls": injected_calls,
              "failed_read_preserved_history": unchanged,
              "selected_rows": selected(history), "raw": raw,
              "source_objects": source_objects(root),
              "original_source_hashes": original_hashes, "original_sources_unchanged": preserved,
              "new_source_names": new_sources,
              "finished_sources_released": recovered not in tb._run_sources and recovered not in tb._registered_runs}
    # A subsequent patch starts fresh discovery even though previous sources
    # and their restart history remain durably available for explicit import.
    public.patch(save=True, root_logdir=str(Path(directory) / "fresh"))
    fresh = sdk.init(id="fresh", project="tb-unpatch", entity="e", dir=directory,
        mode="offline", settings=SETTINGS, _sink=sink)
    result["fresh_directories"] = tb.directories(fresh)
    fresh.finish()
    public.unpatch()
    sdk.teardown()
    return result


def destructive_collision(directory, sink, public):
    """Observe genuine TensorBoardX overwrite bytes and strict replay rejection.

    Main caller: native TensorBoardX probe after the independent restart passes.
    The installed writer gets the same clock second and documented suffix for
    both constructions, reproducing its filename collision deliberately. The
    immutable captures distinguish this unsupported replacement from append and
    restart. No reader, protobuf, source identity or replay check is replaced.
    The rejected group remains pending after Run.log raises. Explicit fixture
    cleanup discards that row only after recording the error and immutable
    journal, then releases the source; normal failed-handle finish is not tested.
    """
    from backprop.integration import tensorboard as tb
    event_module = importlib.import_module("tensorboardX.event_file_writer")
    root = Path(directory) / "collision-events"
    public.patch(save=False, root_logdir=str(root))
    run = sdk.Run(id="collision", project="tb-unpatch", entity="e", dir=directory,
        mode="online", settings=SETTINGS, _sink=sink)
    fixed_second = int(time.time())
    try:
        with patch.object(event_module.time, "time", return_value=fixed_second):
            first = writer(root / "train", filename_suffix=".collision")
            emit(first, 1, {"loss": .25})
            emit(first, 2, {"loss": .5})
            first.close()
        tb.sync(run, root / "train", final=True)
        before_sources, before = source_objects(root), native_events(run)
        with patch.object(event_module.time, "time", return_value=fixed_second):
            replacement = writer(root / "train", filename_suffix=".collision")
            emit(replacement, 1, {"loss": 99.})
            replacement.close()
        after_sources = source_objects(root)
        checkpoint = {"before_source_objects": before_sources, "after_source_objects": after_sources,
                      "physical_events_before_read": before}
        OUTPUT.with_name(OUTPUT.stem + "-collision-before-read.json").write_text(
            json.dumps(checkpoint, indent=2, sort_keys=True) + "\n")
        assert len(before_sources) == len(after_sources) == 1
        assert before_sources[0]["name"] == after_sources[0]["name"]
        assert before_sources[0]["sha256"] != after_sources[0]["sha256"]
        failure = None
        try:
            tb.sync(run, root / "train", final=True)
        except ValueError as error:
            failure = {"type": type(error).__name__, "message": str(error)}
        return {**checkpoint, "error": failure,
                "failed_read_preserved_history": native_events(run) == before,
                "rejected_pending_present": "_tensorboard_group" in run._pending,
                "fixture_cleanup": "Discard rejected pending row after evidence capture, release source, then finish; not failed-handle finish parity"}
    finally:
        public.unpatch()
        tb.release_run(run)
        # This isolated negative fixture must not retry the rejected row during
        # process teardown. Production rejection/pending semantics stay intact.
        with run._lock:
            run._pending.clear()
            run._pending_commits.clear()
        run.finish()
        sdk.teardown()


def lifecycle_preflight():
    """Exercise real non-writer lifecycle stages without installing a decoder.

    Main caller: portable source/native probe acceptance. The genuine writer
    mode uses these same settings, offline/create_new calls, finished core or
    journal capture, and native MemorySink resume. Why this exists: shared probe
    assumptions must fail locally before optional CI reaches writer conversion.
    This branch deliberately makes no TensorBoard ingestion claim.
    """
    with tempfile.TemporaryDirectory(prefix="tensorboard-unpatch-lifecycle-") as directory:
        os.environ["NETRC"] = str(Path(directory) / "netrc")
        os.environ["WANDB_CONFIG_DIR"] = str(Path(directory) / "settings")
        sink = None if REFERENCE else sdk.MemorySink()
        options = {} if REFERENCE else {"_sink": sink}
        first = sdk.init(id="owner", project="tb-unpatch", entity="e", dir=directory,
            mode="offline", settings=SETTINGS, **options)
        second = sdk.init(id="nonglobal", project="tb-unpatch", entity="e", dir=directory,
            mode="offline", settings=SETTINGS, reinit="create_new", **options)
        global_owner = sdk.run is first
        first.log({"lifecycle": 1})
        second.log({"nonglobal": 2})
        second.finish()
        first_alive = not (first._is_finished if REFERENCE else first._closed)
        first.finish()
        first_rows, first_raw = rows(first)
        second_rows, second_raw = rows(second)
        result = {"sdk": sdk.__version__, "boundary": "Actual settings/offline core or journal; no writer or decoder",
            "settings": SETTINGS, "global_owner": global_owner, "primary_survives_secondary_finish": first_alive,
            "first_rows": first_rows, "second_rows": second_rows, "first_raw": first_raw, "second_raw": second_raw}
        if not REFERENCE:
            from backprop.integration import tensorboard as tb
            recovered = sdk.Run(id="owner", project="tb-unpatch", entity="e", dir=directory,
                mode="online", settings=SETTINGS, _sink=sink, resume="must")
            recovered.finish()
            recovered_rows, recovered_raw = rows(recovered)
            fresh = sdk.init(id="fresh", project="tb-unpatch", entity="e", dir=directory,
                mode="offline", settings=SETTINGS, _sink=sink)
            result["restart"] = {"rows": recovered_rows, "raw": recovered_raw,
                "fresh_directories": tb.directories(fresh),
                "finished_sources_released": recovered not in tb._run_sources and recovered not in tb._registered_runs}
            fresh.finish()
        sdk.teardown()
        OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if FAMILY == "lifecycle-preflight":
    lifecycle_preflight()
    raise SystemExit(0)


with tempfile.TemporaryDirectory(prefix="tensorboard-unpatch-") as directory:
    os.environ["NETRC"] = str(Path(directory) / "netrc")
    os.environ["WANDB_CONFIG_DIR"] = str(Path(directory) / "settings")
    public = public_modules[ALIAS]
    root = Path(directory) / "events"
    original, original_init = writer_module.EventFileWriter, writer_module.EventFileWriter.__init__
    patch_return = public.patch(save=False, root_logdir=str(root))
    sink = None if REFERENCE else sdk.MemorySink()
    options = {} if REFERENCE else {"_sink": sink}
    first = sdk.init(id="owner", project="tb-unpatch", entity="e", dir=directory,
        mode="offline", settings=SETTINGS, **options)
    second = sdk.init(id="nonglobal", project="tb-unpatch", entity="e", dir=directory,
        mode="offline", settings=SETTINGS, reinit="create_new", **options)
    current = writer(root / "train")
    emit(current, 1, {"loss": .25})
    emit(current, 2, {"loss": .5})
    if not REFERENCE:
        first.flush()
    unpatch_returns = [public.unpatch(), canonical.unpatch()]
    restored = writer_module.EventFileWriter is original and original.__init__ is original_init
    emit(current, 2, {"loss": .75, "extra": 2.0})
    emit(current, 3, {"loss": 1.0})
    current.close()
    ignored = writer(root / "unannounced")
    emit(ignored, 9, {"ignored": 99.0})
    ignored.close()
    second.finish()
    first.finish()
    first_rows, first_raw = rows(first)
    second_rows, second_raw = rows(second)
    saved_tfevents = (any("tfevents" in path.name for path in Path(first.dir).rglob("*")) if REFERENCE else
        any(event["type"] == "file" and "tfevents" in event["payload"]["name"] for event in native_events(first)))
    source_root = Path(sdk.__file__).parent
    source_names = ["integration/tensorboard/monkeypatch.py"] if REFERENCE else [
        "integration/tensorboard/__init__.py", "integration/tensorboard/reader.py"]
    data = {"sdk": sdk.__version__, "python": sys.version, "family": FAMILY, "alias": ALIAS,
        "runtime_versions": {name: importlib.metadata.version(name) for name in
            (["tensorboard"] + (["torch"] if FAMILY == "torch" else ["tensorboardX"] if FAMILY == "tensorboardx" else []))},
        "source_hashes": {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest() for name in source_names},
        "patch_signature": str(inspect.signature(public.patch)), "patch_return": patch_return,
        "unpatch_returns": unpatch_returns, "aliases": aliases, "constructor_restored": restored,
        "selected_rows": selected(first_rows), "nonglobal_rows": second_rows,
        "unexpected_metric_rows": [row for row in first_rows if any(
            not key.startswith("_") and key != "global_step" and not key.startswith("train/") for key in row)],
        "saved_tfevents": saved_tfevents, "source_objects": source_objects(root),
        "first_raw": first_raw, "second_raw": second_raw}
    OUTPUT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    if not REFERENCE:
        data["restart"] = restart(directory, root, sink, public)
        OUTPUT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        if FAMILY == "tensorboardx":
            data["destructive_collision"] = destructive_collision(directory, sink, public)
            OUTPUT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    sdk.teardown()
