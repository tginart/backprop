"""
Purpose: Mirror one bucket installation into DuckDB for history and live reads.
Input: An object store, an object cache and an optional database file.
Output: The storage interface of LocalDatabase, so the local HTTP server and
    the dashboard work unchanged, plus scoped record reads for the SDK.
Note: Offsets are gap-free and never change. One writer lock assigns them in
    commit order, and readers see only rows at or below the committed boundary.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import threading
import time
from typing import Callable
import uuid

from backprop_backend.local.queries import translate_query
from backprop_backend.records import FIELDS, RecordConflict, canonical_record
from .objects import dependency
from .segments import COLUMNS, run_prefix, time_key, utc_now, write_records


QUERY_RETENTION_S = 3600
ACTIVE_RUN_S = 1800
SWEEP_INTERVAL_S = 300
LISTING_SKEW_S = 600
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    seq BIGINT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_id VARCHAR NOT NULL UNIQUE,
    run_id VARCHAR NOT NULL,
    project VARCHAR NOT NULL,
    entity VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    timestamp VARCHAR NOT NULL,
    payload VARCHAR NOT NULL,
    segment VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (key VARCHAR PRIMARY KEY, status VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS conflicts (event_id VARCHAR NOT NULL, segment VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS imports (id VARCHAR PRIMARY KEY, state VARCHAR NOT NULL);
"""
STAGED = """
CREATE TEMP TABLE staged (
    segment VARCHAR NOT NULL,
    row_index BIGINT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    event_id VARCHAR NOT NULL,
    run_id VARCHAR NOT NULL,
    project VARCHAR NOT NULL,
    entity VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    timestamp VARCHAR NOT NULL,
    payload VARCHAR NOT NULL
)
"""
LOAD_SEGMENT = (f"INSERT INTO staged SELECT ?, file_row_number, {FIELDS} "
                "FROM read_parquet(?, file_row_number = true)")
INSERT_NEW = f"""
INSERT INTO events
SELECT ? + row_number() OVER (ORDER BY segment, row_index), {FIELDS}, segment
FROM (
    SELECT * FROM staged
    WHERE NOT EXISTS (SELECT 1 FROM events WHERE events.event_id = staged.event_id)
    QUALIFY row_number() OVER (PARTITION BY event_id ORDER BY segment, row_index) = 1
)
"""
FIND_CONFLICTS = "SELECT s.event_id, s.segment FROM staged s JOIN events e USING (event_id) WHERE " + \
    " OR ".join(f"s.{name} IS DISTINCT FROM e.{name}" for name in COLUMNS if name != "event_id")
BLOB_PATH = re.compile(r"blobs/([0-9a-f]{64})/[^/\\\x00-\x1f]+")
CLAIM_PATH = re.compile(r"artifact-resolutions/[0-9a-f]{64}\.json")


class BucketMirror:
    """
    Purpose: Own one DuckDB copy of the events in a bucket installation.
    Input: An object store, an ObjectCache, the installation bucket_id and an
        optional database file (None keeps the database in memory).
    Output: Stable offsets, history snapshots, file access and scoped reads.
    """

    def __init__(self, objects, cache, *, bucket_id: str,
                 database_path: Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._duckdb = dependency("duckdb", "bucket")
        self._objects, self._cache, self._clock = objects, cache, clock
        self._db = self._connect(database_path)
        self._db.execute(SCHEMA)
        self._reset_if_moved(bucket_id)
        self.bucket_id = bucket_id
        self.local_id = self._viewer_id()
        self._writer = self._db.cursor()
        self._writer.execute(STAGED)
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._high_water = self._fetch_one("SELECT coalesce(max(seq), 0) FROM events")
        self._seen = {row[0] for row in self._db.execute("SELECT key FROM segments").fetchall()}
        self._runs: dict[str, dict] = {}
        self._queries: dict[str, tuple] = {}
        self._queries_lock = threading.Lock()
        self._stop_refresh = threading.Event()
        self._refresher: threading.Thread | None = None

    # Bucket synchronization

    def refresh(self, *, project: str | None = None, run_id: str | None = None) -> int:
        """
        Purpose: Ingest new segment objects from the bucket.
        Input: An optional project, and an optional run within that project.
        Output: The number of new events.
        Method:
            1. Find the run directories in the scope with delimiter listings.
            2. List each run directory. Active runs list from a lookback start
               key. Each run gets a full listing every SWEEP_INTERVAL_S.
            3. Download the new keys through the cache and ingest them.
        """
        with self._refresh_lock:
            now = self._clock()
            keys = []
            for directory in self._run_directories(project, run_id):
                keys.extend(self._new_keys(directory, now))
            files = self._cache.fetch_many(keys)
            return self._ingest(sorted(files.items()))

    def ingest_written(self, written: list[tuple[str, bytes]]) -> int:
        """Ingest segments that this process wrote, so it reads its own writes."""
        files = [(key, self._cache.store(key, data)) for key, data in written]
        return self._ingest(sorted(files))

    def start_refresher(self, interval_s: float) -> None:
        """
        Purpose: Refresh the mirror on a background thread.
        Input: The interval between refreshes, in seconds.
        Output: A daemon thread. close() stops it.
        Note: A failed refresh is reported on stderr and retried next interval.
        """
        def loop() -> None:
            while not self._stop_refresh.wait(interval_s):
                try:
                    self.refresh()
                except Exception as error:
                    print(f"Backprop bucket refresh failed: {type(error).__name__}: {error}",
                          file=sys.stderr, flush=True)
        self._refresher = threading.Thread(target=loop, name="backprop-bucket-refresh",
                                           daemon=True)
        self._refresher.start()

    def close(self) -> None:
        self._stop_refresh.set()
        if self._refresher is not None:
            self._refresher.join()
        self._writer.close()
        self._db.close()

    # LocalDatabase interface for the HTTP server

    def publish(self, records: list[dict]) -> None:
        """
        Purpose: Write dashboard records to the bucket and make them visible here.
        Input: Stream records from publishRecords.
        Output: Identical duplicates have no effect. Conflicts raise RecordConflict.
        """
        prepared = [canonical_record(record) for record in records]
        with self._lock:
            self._check_conflicts(prepared)
            written = write_records(self._objects, [record for record, _ in prepared],
                                    now=utc_now())
            self.ingest_written(written)

    def end_offset(self) -> int:
        return self._high_water

    def read_records(self, *, start: int, limit: int) -> list[dict]:
        rows = self._fetch_all(
            f"SELECT seq, {FIELDS} FROM events WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT ?",
            [start, self._high_water, limit])
        return [{"offset": str(row[0] - 1), "value": dict(zip(COLUMNS, row[1:]))}
                for row in rows]

    def execute_query(self, query: str) -> str:
        """
        Purpose: Save a history query with its stream boundary.
        Input: A supported Backprop history query.
        Output: A query identifier that expires after QUERY_RETENTION_S.
        Note: seq breaks timestamp ties, so every getResults retry has one order.
        """
        sql, parameters = translate_query(query)
        boundary = self._high_water
        prefix, order = sql.split(" ORDER BY ", 1)
        order = order.removesuffix(" LIMIT ?")
        prefix += " AND seq <= ?" if " WHERE " in prefix else " WHERE seq <= ?"
        snapshot = (time.monotonic(), f"{prefix} ORDER BY {order}, seq LIMIT ?",
                    [*parameters[:-1], boundary, parameters[-1]], boundary)
        identifier = uuid.uuid4().hex
        with self._queries_lock:
            self._prune_queries()
            self._queries[identifier] = snapshot
        return identifier

    def query_page(self, identifier: str) -> tuple[bytes, int]:
        with self._queries_lock:
            self._prune_queries()
            snapshot = self._queries.get(identifier)
        if snapshot is None:
            raise FileNotFoundError("Unknown or expired Backprop history query")
        _, sql, parameters, boundary = snapshot
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(COLUMNS)
        writer.writerows(self._fetch_all(sql, parameters))
        return output.getvalue().encode(), boundary

    def query_result(self, identifier: str) -> bytes:
        return self.query_page(identifier)[0]

    def has_query(self, identifier: str) -> bool:
        with self._queries_lock:
            self._prune_queries()
            return identifier in self._queries

    def cancel_query(self, identifier: str) -> None:
        with self._queries_lock:
            self._queries.pop(identifier, None)

    def upload_file(self, path: str, data: bytes, *, append: bool) -> None:
        """
        Purpose: Store a content-addressed blob or an atomic artifact claim.
        Input: A file path below files/, the bytes and the transaction type.
        Output: A stored object, or FileExistsError for an existing claim.
        """
        blob = BLOB_PATH.fullmatch(path)
        claim = CLAIM_PATH.fullmatch(path)
        if blob:
            if append or blob[1] != hashlib.sha256(data).hexdigest():
                raise ValueError("Artifact bytes do not match their immutable address")
            self._objects.put_content_addressed("files/" + path, data=data)
        elif claim and append:
            if not isinstance(json.loads(data), dict):
                raise ValueError("Artifact resolution must be a JSON object")
            if not self._objects.put_create_only("files/" + path, data=data):
                raise FileExistsError("Artifact resolution already exists")
        else:
            raise ValueError("Unsupported Backprop file path or transaction type")

    def read_file(self, path: str) -> bytes:
        blob = BLOB_PATH.fullmatch(path)
        if blob:
            return self._cache.read_blob("files/" + path, blob[1])
        if CLAIM_PATH.fullmatch(path):
            return self._objects.get("files/" + path)
        raise FileNotFoundError("File not found on the Backprop branch")

    def import_state(self, identifier: str, state: dict | None = None) -> dict:
        """
        Purpose: Keep a journal import cursor for this viewer.
        Input: A source identity and an optional completed prefix descriptor.
        Output: The previous or newly saved descriptor.
        Note: Imports are idempotent by event_id, so a per-viewer cursor is safe.
        """
        with self._lock:
            row = self._writer.execute("SELECT state FROM imports WHERE id = ?",
                                       [identifier]).fetchone()
            previous = json.loads(row[0]) if row else {}
            if state is None:
                return previous
            if previous.get("byte_offset", 0) > state["byte_offset"]:
                return previous
            if (previous.get("byte_offset") == state["byte_offset"]
                    and previous.get("sha256") != state["sha256"]):
                raise RecordConflict("Imported journal prefix changed")
            self._writer.execute("INSERT OR REPLACE INTO imports VALUES (?, ?)",
                                 [identifier, json.dumps(state, sort_keys=True)])
        return state

    # Scoped reads for the SDK store

    def select_records(self, *, project: str | None = None, run_id: str | None = None,
                       entity: str | None = None) -> list[dict]:
        """Return committed records that match the filters, by timestamp and event_id."""
        clauses, parameters = ["seq <= ?"], [self._high_water]
        for column, value in (("project", project), ("run_id", run_id), ("entity", entity)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        rows = self._fetch_all(
            f"SELECT {FIELDS} FROM events WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp, event_id", parameters)
        return [dict(zip(COLUMNS, row)) for row in rows]

    def conflict_count(self) -> int:
        return self._fetch_one("SELECT count(*) FROM conflicts")

    # Private helpers

    def _connect(self, database_path: Path | None):
        if database_path is None:
            return self._duckdb.connect(":memory:")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._duckdb.connect(str(database_path))
        except self._duckdb.IOException as error:
            raise RuntimeError(
                f"The bucket cache {database_path} is in use by another process; "
                "pass a different --cache-dir") from error

    def _reset_if_moved(self, bucket_id: str) -> None:
        row = self._db.execute("SELECT value FROM meta WHERE key = 'bucket_id'").fetchone()
        if row is not None and row[0] != bucket_id:
            for table in ("events", "segments", "conflicts", "imports", "meta"):
                self._db.execute(f"DROP TABLE {table}")
            self._db.execute(SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta VALUES ('bucket_id', ?)", [bucket_id])

    def _viewer_id(self) -> str:
        self._db.execute("INSERT OR IGNORE INTO meta VALUES ('viewer_id', ?)", [uuid.uuid4().hex])
        return self._db.execute("SELECT value FROM meta WHERE key = 'viewer_id'").fetchone()[0]

    def _run_directories(self, project: str | None, run_id: str | None) -> list[str]:
        """
        Purpose: Find the run directories in the events and control trees.
        Input: An optional project and run.
        Output: Relative prefixes that end in run_id=<q>/.
        """
        directories = []
        for kind in ("events", "control"):
            if project is not None and run_id is not None:
                directories.append(run_prefix(kind, project, run_id))
                continue
            if project is not None:
                projects = [run_prefix(kind, project)]
            else:
                projects = self._objects.list(kind + "/", delimiter="/")[1]
            for prefix in projects:
                directories.extend(self._objects.list(prefix, delimiter="/")[1])
        return directories

    def _new_keys(self, directory: str, now: float) -> list[str]:
        """
        Purpose: List the unseen segment keys of one run directory.
        Input: A run directory and the current time of this reader.
        Output: Unseen .parquet keys.
        Note: The lookback start uses this reader's clock, not writer clocks.
            The full sweep catches uploads that finish after the lookback.
        """
        state = self._runs.get(directory)
        is_full = (state is None or now - state["full_at"] >= SWEEP_INTERVAL_S
                   or directory.startswith("control/"))
        if not is_full and now - state["new_at"] > ACTIVE_RUN_S:
            return []
        start = None
        if not is_full:
            moment = datetime.fromtimestamp(state["listed_at"] - LISTING_SKEW_S, timezone.utc)
            start = directory + time_key(moment)
        keys, _ = self._objects.list(directory, start_offset=start)
        new = [key for key in keys if key.endswith(".parquet") and key not in self._seen]
        state = state or {"full_at": now, "new_at": 0.0}
        if is_full:
            state["full_at"] = now
        state["listed_at"] = now
        if new:
            state["new_at"] = now
        self._runs[directory] = state
        return new

    def _ingest(self, files: list[tuple[str, Path]]) -> int:
        """
        Purpose: Add the events of local segment files with new offsets.
        Input: Segment keys and their local files, sorted by key.
        Output: The number of new events.
        Method:
            1. Load each unseen file into the staged table. A file that DuckDB
               cannot read is marked invalid and reported.
            2. In one transaction, give each new event_id the next offset in
               (segment, row) order, record conflicts and record the segments.
            3. After the commit, advance the boundary that readers use.
        """
        with self._lock:
            loaded, invalid = [], []
            for key, path in files:
                if key in self._seen:
                    continue
                try:
                    self._writer.execute(LOAD_SEGMENT, [key, str(path)])
                    loaded.append(key)
                except self._duckdb.Error as error:
                    invalid.append(key)
                    print(f"Backprop bucket segment {key} is invalid and was skipped: {error}",
                          file=sys.stderr, flush=True)
            if not loaded and not invalid:
                return 0
            try:
                self._writer.execute("BEGIN TRANSACTION")
                self._writer.execute(INSERT_NEW, [self._high_water])
                conflicts = self._writer.execute(FIND_CONFLICTS).fetchall()
                if conflicts:
                    self._writer.executemany("INSERT INTO conflicts VALUES (?, ?)", conflicts)
                self._writer.executemany(
                    "INSERT OR IGNORE INTO segments VALUES (?, ?)",
                    [(key, "ok") for key in loaded] + [(key, "invalid") for key in invalid])
                high_water = self._writer.execute(
                    "SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]
                self._writer.execute("COMMIT")
            except BaseException:
                self._writer.execute("ROLLBACK")
                raise
            finally:
                self._writer.execute("DELETE FROM staged")
            added, self._high_water = high_water - self._high_water, high_water
            self._seen.update(loaded + invalid)
        if conflicts:
            print(f"Backprop bucket kept the first copy of {len(conflicts)} conflicting "
                  "event IDs", file=sys.stderr, flush=True)
        return added

    def _check_conflicts(self, prepared: list[tuple[dict, str]]) -> None:
        records: dict[str, dict] = {}
        for record, _ in prepared:
            if records.setdefault(record["event_id"], record) != record:
                raise RecordConflict("Conflicting content for an existing event ID")
        if not records:
            return
        rows = self._fetch_all(
            f"SELECT {FIELDS} FROM events WHERE event_id IN (SELECT unnest(?))",
            [list(records)])
        for row in rows:
            stored = dict(zip(COLUMNS, row))
            if stored != records[stored["event_id"]]:
                raise RecordConflict("Conflicting content for an existing event ID")

    def _prune_queries(self) -> None:
        expired = time.monotonic() - QUERY_RETENTION_S
        for identifier in [key for key, value in self._queries.items() if value[0] <= expired]:
            del self._queries[identifier]

    def _fetch_all(self, sql: str, parameters: list | None = None) -> list[tuple]:
        with self._db.cursor() as cursor:
            return cursor.execute(sql, parameters or []).fetchall()

    def _fetch_one(self, sql: str, parameters: list | None = None):
        with self._db.cursor() as cursor:
            return cursor.execute(sql, parameters or []).fetchone()[0]
