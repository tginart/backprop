"""
Purpose: Commit Backprop events and artifact references to durable local storage.
Input: Canonical stream records, immutable files, and history queries.
Output: Stable offsets and committed records after a SQLite transaction.
Note: Each operation owns its connection. WAL permits concurrent readers.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
import csv
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from .queries import FIELDS, translate_query


MAX_RECORD_BYTES = 900_000
QUERY_RETENTION_S = 3600
SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    offset INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    project TEXT NOT NULL,
    entity TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload TEXT NOT NULL,
    canonical TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_scope ON events(project, entity, run_id);
CREATE INDEX IF NOT EXISTS events_order ON events(timestamp, event_id);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS queries (
    id TEXT PRIMARY KEY, result BLOB NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    id TEXT PRIMARY KEY, state TEXT NOT NULL
);
"""


class LocalConflict(ValueError):
    """The same immutable identity has different content."""


class LocalDatabase:
    """
    Purpose: Own one installation's SQLite database and content-addressed files.
    Input: A directory that belongs to this local Backprop installation.
    Output: Durable event, history, artifact, and import operations.
    """

    def __init__(self, directory: str | Path) -> None:
        """
        Purpose: Initialize the database schema and stable installation identity.
        Input: A local data directory.
        Output: A database with WAL and FULL commit durability.
        """
        self.directory = Path(directory).expanduser().resolve()
        missing = []
        parent = self.directory
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        self.directory.mkdir(parents=True, exist_ok=True)
        for parent in reversed(missing):
            _sync_directory(parent.parent)
        self.artifacts = self.directory / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        self.path = self.directory / "backprop.sqlite3"
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("This local database requires a newer Backprop version")
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(queries)")
            }
            if "created_at" not in columns:
                connection.execute(
                    "ALTER TABLE queries ADD COLUMN created_at REAL NOT NULL DEFAULT 0"
                )
                connection.execute("UPDATE queries SET created_at=?", (time.time(),))
            connection.execute(
                "CREATE INDEX IF NOT EXISTS queries_created ON queries(created_at)"
            )
            connection.execute(
                "DELETE FROM queries WHERE created_at<=?",
                (time.time() - QUERY_RETENTION_S,),
            )
            connection.execute("PRAGMA user_version=2")
            connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('local_id', ?)",
                (uuid.uuid4().hex,),
            )
            self.local_id = connection.execute(
                "SELECT value FROM metadata WHERE key='local_id'"
            ).fetchone()[0]
        _sync_directory(self.directory)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """
        Purpose: Close every connection after its transaction completes.
        Input: The database path and the fixed durability settings.
        Output: A connection with FULL durability and a bounded lock wait.
        """
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def publish(self, records: list[dict]) -> None:
        """
        Purpose: Commit a whole upload or reject it without partial records.
        Input: Stream records with stable event identifiers.
        Output: Identical duplicates have no effect. Conflicts raise LocalConflict.
        """
        prepared = [_canonical_record(record) for record in records]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for record, canonical in prepared:
                previous = connection.execute(
                    "SELECT canonical FROM events WHERE event_id=?",
                    (record["event_id"],),
                ).fetchone()
                if previous is not None:
                    if previous[0] != canonical:
                        raise LocalConflict("Conflicting content for an existing event ID")
                    continue
                connection.execute(
                    f"INSERT INTO events ({FIELDS},canonical) VALUES (?,?,?,?,?,?,?,?,?)",
                    [*[record[name] for name in FIELDS.split(",")], canonical],
                )

    def end_offset(self) -> int:
        with self.connect() as connection:
            return connection.execute(
                "SELECT COALESCE(MAX(offset),0) FROM events"
            ).fetchone()[0]

    def read_records(self, *, start: int, limit: int) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT offset,{FIELDS} FROM events WHERE offset>?"
                " ORDER BY offset LIMIT ?", (start, limit),
            ).fetchall()
        return [{"offset": str(row["offset"] - 1),
                 "value": {name: row[name] for name in FIELDS.split(",")}}
                for row in rows]

    def execute_query(self, query: str) -> str:
        """
        Purpose: Save a stable history boundary within a bounded retry window.
        Input: A supported Backprop history query.
        Output: A durable query identifier that expires after one hour.
        """
        sql, parameters = translate_query(query)
        identifier = uuid.uuid4().hex
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM queries WHERE created_at<=?", (now - QUERY_RETENTION_S,)
            )
            boundary = connection.execute(
                "SELECT COALESCE(MAX(offset),0) FROM events"
            ).fetchone()[0]
            prefix, order = sql.split(" ORDER BY ", 1)
            prefix += " AND offset<=?" if " WHERE " in prefix else " WHERE offset<=?"
            snapshot = {"sql": prefix + " ORDER BY " + order,
                        "parameters": [*parameters[:-1], boundary, parameters[-1]],
                        "end_offset": boundary}
            connection.execute(
                "INSERT INTO queries (id,result,created_at) VALUES (?,?,?)",
                (identifier, json.dumps(snapshot).encode(), now),
            )
        return identifier

    def query_result(self, identifier: str) -> bytes:
        return self.query_page(identifier)[0]

    def query_page(self, identifier: str) -> tuple[bytes, int]:
        """
        Purpose: Return the stable CSV page and its exact stream boundary.
        Input: A committed query identifier within the retry window.
        Output: Immutable rows and the next offset after those rows.
        Note: Read transactions permit concurrent event writers.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT result FROM queries WHERE id=? AND created_at>?",
                (identifier, time.time() - QUERY_RETENTION_S),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("Unknown or expired Backprop history query")
            snapshot = json.loads(row[0])
            rows = connection.execute(snapshot["sql"], snapshot["parameters"])
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(FIELDS.split(","))
            writer.writerows(tuple(row) for row in rows)
        boundary = snapshot.get("end_offset", snapshot["parameters"][-2])
        return output.getvalue().encode(), boundary

    def has_query(self, identifier: str) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM queries WHERE id=? AND created_at>?",
                (identifier, time.time() - QUERY_RETENTION_S),
            ).fetchone() is not None

    def cancel_query(self, identifier: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM queries WHERE id=?", (identifier,))

    def upload_file(self, path: str, data: bytes, *, append: bool) -> None:
        """
        Purpose: Commit immutable bytes before the file reference becomes visible.
        Input: A content-addressed blob or an atomic artifact-resolution claim.
        Output: A durable reference, or a conflict for an existing APPEND claim.
        Method:
            1. Validate the address and write the bytes with fsync.
            2. Commit the reference with a SQLite transaction.
            3. Return only after the transaction completes.
        """
        digest = hashlib.sha256(data).hexdigest()
        blob = re.fullmatch(r"blobs/([0-9a-f]{64})/[^/\\\x00-\x1f]+", path)
        claim = re.fullmatch(r"artifact-resolutions/[0-9a-f]{64}\.json", path)
        if blob:
            if append or blob[1] != digest:
                raise ValueError("Artifact bytes do not match their immutable address")
        elif claim and append:
            if not isinstance(json.loads(data), dict):
                raise ValueError("Artifact resolution must be a JSON object")
        else:
            raise ValueError("Unsupported Backprop file path or transaction type")
        destination = self.artifacts / digest
        _write_durable(destination, data)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT sha256,size FROM files WHERE path=?", (path,)
            ).fetchone()
            if previous is not None:
                if append:
                    raise FileExistsError("Artifact resolution already exists")
                if (previous[0], previous[1]) != (digest, len(data)):
                    raise LocalConflict("Conflicting content at an immutable file path")
            else:
                connection.execute(
                    "INSERT INTO files VALUES (?,?,?)", (path, digest, len(data))
                )

    def read_file(self, path: str) -> bytes:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT sha256,size FROM files WHERE path=?", (path,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError("File not found on the Backprop branch")
        data = (self.artifacts / row[0]).read_bytes()
        if len(data) != row[1] or hashlib.sha256(data).hexdigest() != row[0]:
            raise OSError("Stored artifact failed its integrity check")
        return data

    def import_state(self, identifier: str, state: dict | None = None) -> dict:
        """
        Purpose: Keep a local ingestion cursor separate from source journals.
        Input: A source identity and an optional completed prefix descriptor.
        Output: The previous or newly committed local ingestion descriptor.
        """
        with self.connect() as connection:
            if state is not None:
                connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM imports WHERE id=?", (identifier,)
            ).fetchone()
            previous = json.loads(row[0]) if row else {}
            if state is None:
                return previous
            if previous.get("byte_offset", 0) > state["byte_offset"]:
                return previous
            if (previous.get("byte_offset") == state["byte_offset"]
                    and previous.get("sha256") != state["sha256"]):
                raise LocalConflict("Imported journal prefix changed")
            connection.execute(
                "INSERT INTO imports VALUES (?,?) ON CONFLICT(id)"
                " DO UPDATE SET state=excluded.state",
                (identifier, json.dumps(state, sort_keys=True)),
            )
        return state


def _canonical_record(record: dict) -> tuple[dict, str]:
    """
    Purpose: Validate one event record before any database change.
    Input: The stable tabular Backprop schema.
    Output: A detached record and its canonical identity representation.
    """
    if not isinstance(record, dict) or set(record) != set(FIELDS.split(",")):
        raise ValueError("Invalid Backprop stream record schema")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("Unsupported Backprop event schema version")
    for name in ("event_id", "run_id", "project", "entity", "event_type", "timestamp"):
        if not isinstance(record[name], str) or (name != "entity" and not record[name]):
            raise ValueError("Invalid Backprop event identity")
    datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    if not isinstance(record["payload"], str):
        raise ValueError("Backprop record payload must be encoded JSON")
    payload = json.loads(record["payload"])
    if not isinstance(payload, dict):
        raise ValueError("Backprop event payload must be a JSON object")
    record = {**record, "payload": json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )}
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(canonical.encode()) > MAX_RECORD_BYTES:
        raise ValueError("Backprop event exceeds the 900 KB record limit")
    return record, canonical


def _write_durable(destination: Path, data: bytes) -> None:
    """
    Purpose: Make immutable artifact bytes durable before a database reference.
    Input: A hash path and the complete file content.
    Output: A flushed file and directory entry.
    """
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            output.close()
            os.replace(temporary, destination)
            _sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
