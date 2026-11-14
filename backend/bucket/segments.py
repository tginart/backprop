"""
Purpose: Define the bucket layout and convert event batches to Parquet segments.
Input: Canonical Backprop stream records and an object store.
Output: Immutable segment objects with deterministic, time-ordered keys.
Note: Layout under the installation prefix:
    _backprop/installation.json
    events/project=<q>/run_id=<q>/<time>-<sha256>.parquet
    control/project=<q>/run_id=<q>/instance=<q>/<time>-<sha256>.parquet
    files/blobs/<sha256>/<name>
    files/artifact-resolutions/<key>.json
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import quote
import uuid

from backprop_backend.records import FIELDS, canonical_record
from .objects import BucketError, dependency


INSTALLATION_KEY = "_backprop/installation.json"
SCHEMA_VERSION = 1
CONTROL_TYPE = "run_stop_request"
TIME_FORMAT = "%Y%m%dT%H%M%S%fZ"
COLUMNS = FIELDS.split(",")
COLUMN_TYPES = ("INTEGER",) + ("VARCHAR",) * (len(COLUMNS) - 1)


def ensure_installation(objects) -> dict:
    """
    Purpose: Read the installation identity, and create it if it is absent.
    Input: An object store for one installation prefix.
    Output: The descriptor with a 32-hex bucket_id and the schema version.
    Note: A create-only write makes concurrent first writers agree on one ID.
    """
    try:
        data = objects.get(INSTALLATION_KEY)
    except FileNotFoundError:
        candidate = {"bucket_id": uuid.uuid4().hex, "schema_version": SCHEMA_VERSION}
        objects.put_create_only(INSTALLATION_KEY, data=json.dumps(candidate).encode())
        data = objects.get(INSTALLATION_KEY)
    descriptor = json.loads(data)
    if not isinstance(descriptor, dict) or descriptor.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("This bucket installation requires a newer Backprop version")
    if not re.fullmatch(r"[0-9a-f]{32}", str(descriptor.get("bucket_id", ""))):
        raise ValueError("The bucket installation descriptor is invalid")
    return descriptor


def run_prefix(kind: str, project: str, run_id: str | None = None) -> str:
    """Return the events/ or control/ prefix for one project or one run."""
    assert kind in ("events", "control")
    prefix = f"{kind}/project={quote(project, safe='')}/"
    if run_id is not None:
        prefix += f"run_id={quote(run_id, safe='')}/"
    return prefix


def control_prefix(project: str, run_id: str, instance_id: str) -> str:
    return run_prefix("control", project, run_id) + f"instance={quote(instance_id, safe='')}/"


def time_key(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def write_records(objects, records: list[dict], *, now: datetime) -> list[tuple[str, bytes]]:
    """
    Purpose: Validate records and write one create-only segment per group.
    Input: Stream records, an object store and the writer's current time.
    Output: The key and Parquet bytes of each segment, in write order.
    Method:
        1. Validate each record with canonical_record. Stop before any write
           if one record is invalid.
        2. Group the records by run, and stop requests by process instance.
        3. Name each segment by the time and the SHA-256 of its records.
        4. Write each segment. An existing key holds the same records.
    """
    groups = group_records([canonical_record(record) for record in records])
    written = []
    for prefix, rows in groups.items():
        content = "\n".join(canonical for _, canonical in rows)
        digest = hashlib.sha256(content.encode()).hexdigest()
        key = f"{prefix}{time_key(now)}-{digest}.parquet"
        data = encode_segment([record for record, _ in rows])
        objects.put_create_only(key, data=data)
        written.append((key, data))
    return written


def group_records(prepared: list[tuple[dict, str]]) -> dict[str, list[tuple[dict, str]]]:
    """
    Purpose: Assign each validated record to its segment prefix.
    Input: Records with their canonical JSON, from canonical_record.
    Output: An ordered map from segment prefix to its records.
    Note: A stop request without an instance ID cannot reach a process, so it
        goes to the events prefix with the other run events.
    """
    groups: dict[str, list[tuple[dict, str]]] = {}
    for record, canonical in prepared:
        instance = None
        if record["event_type"] == CONTROL_TYPE:
            instance = json.loads(record["payload"]).get("instance_id")
        if isinstance(instance, str) and instance:
            prefix = control_prefix(record["project"], record["run_id"], instance)
        else:
            prefix = run_prefix("events", record["project"], record["run_id"])
        groups.setdefault(prefix, []).append((record, canonical))
    return groups


def encode_segment(records: list[dict]) -> bytes:
    """
    Purpose: Encode records as one zstd-compressed Parquet file.
    Input: Validated stream records.
    Output: The Parquet bytes, with the records.FIELDS columns in order.
    """
    duckdb = dependency("duckdb", "bucket")
    connection = duckdb.connect()
    try:
        columns = ", ".join(f'"{name}" {kind}' for name, kind in zip(COLUMNS, COLUMN_TYPES))
        connection.execute(f"CREATE TABLE segment ({columns})")
        placeholders = ",".join("?" * len(COLUMNS))
        connection.executemany(f"INSERT INTO segment VALUES ({placeholders})",
                               [[record[name] for name in COLUMNS] for record in records])
        with tempfile.TemporaryDirectory(prefix="backprop-segment-") as directory:
            path = Path(directory) / "segment.parquet"
            connection.execute(f"COPY segment TO {_sql_path(path)} "
                               "(FORMAT parquet, COMPRESSION zstd)")
            return path.read_bytes()
    finally:
        connection.close()


def decode_segment(data: bytes) -> list[dict]:
    """
    Purpose: Read the records of one Parquet segment.
    Input: The Parquet bytes of a segment object.
    Output: Records with the records.FIELDS keys, in file order.
    """
    duckdb = dependency("duckdb", "bucket")
    with tempfile.TemporaryDirectory(prefix="backprop-segment-") as directory:
        path = Path(directory) / "segment.parquet"
        path.write_bytes(data)
        connection = duckdb.connect()
        try:
            columns = ", ".join(f'"{name}"' for name in COLUMNS)
            rows = connection.execute(
                f"SELECT {columns} FROM read_parquet({_sql_path(path)})").fetchall()
        except duckdb.Error as error:
            raise BucketError("A bucket segment is not a valid Backprop Parquet file",
                              retryable=False) from error
        finally:
            connection.close()
    return [dict(zip(COLUMNS, row)) for row in rows]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sql_path(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"
