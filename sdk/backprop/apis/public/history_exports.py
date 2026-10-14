"""Export a consistent event snapshot as one atomic, scoped Parquet file.

Architecture: validate portable arguments -> refresh one run snapshot -> check
recorded writers -> hydrate/encode -> atomically replace a destination file.
Main caller: Public Run.download_history_exports. Setup/query failures raise;
file production failures appear in DownloadHistoryResult.errors. There is no
background export service or new persistent resource. Foundry exports currently
visible rows, including live rows when the caller explicitly permits them.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import operator
from pathlib import Path
import tempfile
from urllib.parse import quote

from ...errors import CommError
from .history import DownloadHistoryResult, IncompleteRunHistoryError, parquet_table
from .replay import hydrate


def incomplete_writers(writers, events):
    """Find announced shared writers whose observed record chain is incomplete.

    Main callers: contains_live_history and shared Python/browser fixtures. A
    finish record can become visible before an earlier record, so terminal state
    alone cannot establish completeness. Count sequence values without building
    a range; repeated event IDs are retries, conflicting IDs at one sequence are
    ambiguous. Unknown/invalid sequence metadata remains explicitly incomplete.
    """
    sequences, invalid = {}, set()
    for event in events:
        writer = event["payload"].get("_writer")
        if not writer: continue
        identity, sequence = writer["id"], writer.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or not 0 <= sequence < 2**53:
            invalid.add(identity)
            continue
        records = sequences.setdefault(identity, {}).setdefault(sequence, {})
        records[event["event_id"]] = event["type"]
    result = []
    for identity, writer in writers.items():
        observed = sequences.get(identity, {})
        last = max(observed, default=-1)
        missing = last + 1 - len(observed)
        ambiguous = identity in invalid or any(len(records) != 1 for records in observed.values())
        finished_last = bool(observed) and all(kind in {"run_finish", "writer_finish"} for kind in observed[last].values())
        if writer["state"] in {"running", "pending"} or missing or ambiguous or not finished_last:
            result.append({"id": identity, "label": (writer.get("label") if writer.get("label") is not None else identity), "state": writer["state"],
                           "missing_records": missing, "invalid_sequence": ambiguous, "finish_is_last": finished_last})
    return result


def contains_live_history(state, events):
    """Keep active, gapped or unfinished shared contributions visibly provisional.

    Main caller: export_history. The primary owns recorded run state, but a
    snapshot is still provisional until each announced writer's complete record
    chain ends with completion. A writer that never finishes remains incomplete;
    require_complete_history=False explicitly exports the currently visible rows.
    """
    return state["state"] in {"running", "pending"} or bool(incomplete_writers(state["writers"], events))


def export_history(run, download_dir, require_complete_history=True):
    """Prepare one snapshot, then return the paths or errors from writing it.

    Main caller: Public Run.download_history_exports. The input conversions match
    the pinned wrapper/protobuf boundary without importing its service protocol.
    Package and directory errors happen before starting the file operation. An
    existing export survives any later encoding/write/replace failure. Different
    entity/project/run scopes cannot accidentally share the destination name.
    """
    directory = str(download_dir)
    flag = 0 if require_complete_history is None else operator.index(require_complete_history)
    # The pinned protobuf boundary uses a C long before converting to bool.
    limit = 1 << (ctypes.sizeof(ctypes.c_long) * 8 - 1)
    if not -limit <= flag < limit:
        raise OverflowError("Python int too large to convert to C long")
    require_complete = bool(flag)
    run.load(force=True)
    state = run._ensure()
    live = contains_live_history(state, run._events)
    if live and require_complete:
        raise IncompleteRunHistoryError()
    try:
        import pyarrow as arrow
        import pyarrow.parquet as parquet
    except ModuleNotFoundError as error:
        if error.name != "pyarrow":
            raise
        raise ImportError("Parquet export requires the optional pyarrow package; scan_history requires no additional dependency") from None
    root = Path(directory)
    try:
        if directory == "":
            raise FileNotFoundError("History export directory cannot be empty")
        root.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as error:
        raise CommError("Failed to prepare history export directory: " + str(error), error) from error
    scope = hashlib.sha256(json.dumps(run.path, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]
    destination = root / f"{quote(run.id, safe='')}-{scope}-history.parquet"
    temporary, failure = None, None
    try:
        rows = hydrate(state["history"], run._store, run._blobs)
        table = parquet_table(rows, arrow)
        with tempfile.NamedTemporaryFile(dir=root, suffix=".parquet", delete=False) as output:
            temporary = Path(output.name)
        parquet.write_table(table, temporary)
        temporary.replace(destination)
    except Exception as error:
        # This field is the public export contract's explicit per-file failure
        # channel. Initial query/import/setup failures above still raise.
        failure = str(error)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                cleanup = "Temporary file cleanup failed: " + str(error)
                failure = cleanup if failure is None else failure + "; " + cleanup
    if failure is not None:
        return DownloadHistoryResult([], live, {destination: failure})
    return DownloadHistoryResult([destination], live, {})
