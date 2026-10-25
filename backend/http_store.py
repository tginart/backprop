"""
Purpose: Share Backprop's existing HTTP event, history, file, and retry contract.
Input: A backend adapter with a validated origin and request credentials.
Output: Durable uploads and canonical event and artifact reads.
Note: Backend adapters own authentication and backend-specific error policies.
"""
from __future__ import annotations

import base64
from collections.abc import Iterator
import csv
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import http.client
import io
import json
import mimetypes
from pathlib import Path
import re
import time
import zlib
from urllib import error, parse, request

from backprop.errors import CommError
from backprop.failures import retryable_failure, retryable_status


class BackendError(CommError):
    """An explicit HTTP or storage-contract failure without credential content."""

    def __init__(self, message, *, status_code=None, retryable=False, response_truncated=False, error_name=None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.response_truncated = response_truncated
        self.error_name = error_name


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpStore:
    """Read and write the two resources provisioned for a Backprop installation.

    Main callers: the SDK. Pass a config mapping with host,
    stream_dataset_rid, media_dataset_rid, and optionally branch/stream_view_rid.
    Backend adapters supply the origin and authentication policy.
    """

    _archive_page_size = 100_000
    _live_read_workers = 1
    _accept_compression = False
    backend_label = "Backprop"
    error_type = BackendError

    def _upload_transaction_conflict(self, path, method, status, detail):
        return False



    def request(self, path, method="GET", body=None, content_type="application/json", *, retry=False, raw=False):
        """Send one authenticated same-origin request and surface failures clearly.

        Main callers: publish, read_events, upload_file. Only safe reads and
        application-idempotent event/file writes opt into transient retries.
        """
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Backend requests must use a relative API path")
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        attempts = 4 if retry else 1
        for attempt in range(attempts):
            headers = {"Content-Type": content_type}
            if self._accept_compression:
                headers["Accept-Encoding"] = "gzip"
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            req = request.Request(self.host + path, data=body, method=method,
                                  headers=headers)
            try:
                with self.opener.open(req, timeout=self.config.get("timeout", 60)) as response:
                    data = response.read()
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        try:
                            data = gzip.decompress(data)
                        except (EOFError, gzip.BadGzipFile, zlib.error):
                            raise http.client.IncompleteRead(data) from None
                    if raw:
                        return data
                    if not data:
                        return None
                    if "json" in response.headers.get("Content-Type", ""):
                        return json.loads(data)
                    return data
            except error.HTTPError as exc:
                can_retry = retryable_status(exc.code)
                try:
                    try:
                        if (exc.headers or {}).get("Content-Encoding", "").lower() == "gzip":
                            with gzip.GzipFile(fileobj=exc) as body:
                                detail = body.read(1000).decode("utf-8", errors="replace")
                        else:
                            detail = exc.read(1000).decode("utf-8", errors="replace")
                        can_retry = can_retry or self._upload_transaction_conflict(path, method, exc.code, detail)
                        if self.token:
                            detail = detail.replace(self.token, "[redacted]")
                    except (OSError, http.client.HTTPException, EOFError, zlib.error) as body_error:
                        # The response status is authoritative. A truncated or
                        # slow diagnostic body must not turn a denied request
                        # into a retryable connection failure.
                        detail = f"Error response body unavailable ({type(body_error).__name__})"
                finally:
                    exc.close()
                if retry and can_retry and attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                try:
                    diagnostic = json.loads(detail)
                    error_name = diagnostic.get("errorName") if isinstance(diagnostic, dict) else None
                except ValueError:
                    error_name = None
                raise self.error_type(f"{self.backend_label} {method} failed (HTTP {exc.code}): {detail}",
                                  status_code=exc.code, retryable=can_retry, error_name=error_name) from None
            except (error.URLError, OSError, http.client.HTTPException) as exc:
                can_retry = retryable_failure(exc)
                if retry and can_retry and attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise self.error_type(f"{self.backend_label} {method} connection failed: {type(exc).__name__}",
                                  retryable=can_retry,
                                  response_truncated=isinstance(exc, http.client.IncompleteRead)) from None


    @staticmethod
    def event_to_record(event):
        """Flatten an SDK envelope into the stable, tabular stream schema."""
        return {"schema_version": event["schema_version"], "event_id": event["event_id"],
                "run_id": event["run_id"], "project": event["project"],
                "entity": event.get("entity") or "", "event_type": event["type"],
                "timestamp": event["timestamp"],
                "payload": json.dumps(event["payload"], separators=(",", ":"), allow_nan=False)}

    @staticmethod
    def record_to_event(record):
        """Restore the same envelope from either CSV archive rows or stream rows."""
        return {"schema_version": int(record["schema_version"]), "event_id": record["event_id"],
                "run_id": record["run_id"], "project": record["project"],
                "entity": record["entity"], "type": record["event_type"],
                "timestamp": record["timestamp"], "payload": json.loads(record["payload"])}

    def publish(self, events):
        """Publish event batches with stable IDs and an explicit per-event size cap.

        Main callers: the SDK.flush. Images and large files must be uploaded
        separately; this prevents one rich value from overwhelming stream limits.
        """
        batch, size = [], 0
        for event in events:
            record = self.event_to_record(event)
            record_size = len(json.dumps(record, ensure_ascii=True).encode())
            if record_size > 900_000:
                raise ValueError("Logging event exceeds 900 KB; split the table or upload a file")
            if batch and (size + record_size > 2_000_000 or len(batch) >= 500):
                self._publish_batch(batch)
                batch, size = [], 0
            batch.append(record)
            size += record_size
        if batch:
            self._publish_batch(batch)

    def _publish_batch(self, records):
        body = {"records": records}
        if self.config.get("stream_view_rid"):
            body["viewRid"] = self.config["stream_view_rid"]
        self.request(self.stream_path + "/publishRecords", "POST", body, retry=True)

    def _stream_query(self, **params):
        params["preview"] = "true"
        if self.config.get("stream_view_rid"):
            params["viewRid"] = self.config["stream_view_rid"]
        return parse.urlencode(params)

    def read_live_records(self) -> Iterator[dict]:
        """
        Purpose: Read a fixed stream snapshot with bounded parallel page requests.
        Input: Captured partition ends and the backend's page and worker limits.
        Output: Ordered records without gaps, duplicate offsets, or records beyond the snapshot.
        Note: Sparse offsets and partial pages fall back to the next actual cursor.
        """
        ends = self.request(self.stream_path + "/getEndOffsets?" + self._stream_query(), retry=True)
        with ThreadPoolExecutor(max_workers=self._live_read_workers) as workers:
            for partition, boundary in ends.items():
                offset, end = None, int(boundary)
                while end > 0 and (offset is None or offset < end):
                    starts = ([None] if offset is None else
                              list(range(offset, min(end, offset + self._live_page_size * self._live_read_workers),
                                         self._live_page_size)))
                    page_size = self._live_page_size
                    def fetch(start):
                        return self._read_live_page(partition, start, page_size=page_size)
                    pages = list(workers.map(fetch, starts))
                    exhausted = False
                    for start, (rows, size) in zip(starts, pages):
                        self._live_page_size = min(self._live_page_size, size)
                        if start is not None and offset is not None and start > offset:
                            break
                        if not rows:
                            exhausted = True
                            break
                        previous = offset
                        for row in rows:
                            position = int(row["offset"])
                            if (offset is None or position >= offset) and position < end:
                                yield row["value"]
                        next_offset = max(int(row["offset"]) for row in rows) + 1
                        if previous is not None and start == previous and next_offset <= previous:
                            raise self.error_type("The backend returned non-advancing stream offsets")
                        offset = max(previous or 0, next_offset)
                    if exhausted:
                        break

    def _read_live_page(self, partition: str, offset: int | None,
                        *, page_size: int) -> tuple[list[dict], int]:
        """
        Purpose: Retry a truncated page at the same offset with a smaller limit.
        Input: One partition cursor and an initial record limit.
        Output: A complete page and its successful limit.
        Note: Each HTTP request retains the existing four-attempt retry policy.
        """
        while True:
            params = {"partitionId": partition, "limit": page_size}
            if offset is not None:
                params["startOffset"] = str(offset)
            try:
                rows = self.request(self.stream_path + "/getRecords?" + self._stream_query(**params), retry=True)
                return rows, page_size
            except BackendError as error:
                if not error.response_truncated or page_size == 1:
                    raise
                page_size = max(1, page_size // 2)

    @staticmethod
    def _sql_text(value):
        return "decode(unhex('" + str(value).encode().hex() + "'), 'UTF-8')"

    def query_archive(self, project=None, run_id=None, entity=None):
        """Fetch durable archived events, scoped by exact UTF-8 string comparisons.

        Main callers: read_events and resume. Why this exists: live stream storage
        is not treated as indefinite history. SQL filters avoid replaying unrelated
        archived runs; hex literals keep caller-controlled values out of SQL syntax.
        """
        filters = []
        for column, value in (("project", project), ("run_id", run_id), ("entity", entity)):
            if value is not None:
                filters.append(f"{column} = {self._sql_text(value)}")
        rows, cursor = [], None
        page_size = self._archive_page_size
        while True:
            clauses = list(filters)
            if cursor is not None:
                stamp, identifier = map(self._sql_text, cursor)
                clauses.append(f"(timestamp < {stamp} OR (timestamp = {stamp} AND event_id < {identifier}))")
            sql = f"SELECT * FROM `{self.stream_rid}`"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            # Descending pages cannot chase an active writer indefinitely. Event
            # ID breaks equal timestamps; identical retry IDs are deduplicated.
            sql += f" ORDER BY timestamp DESC, event_id DESC LIMIT {page_size}"
            page = self._query_csv(sql)
            rows.extend(page)
            if len(page) < page_size:
                return rows
            next_cursor = (page[-1]["timestamp"], page[-1]["event_id"])
            if cursor is not None and next_cursor >= cursor:
                raise self.error_type("Backend archive query returned a non-advancing page")
            cursor = next_cursor

    def _query_csv(self, sql, *, timeout=180, retry=True):
        """Run a scoped SQL page with a deadline and explicit failure reporting."""
        status = self.request("/api/v2/sqlQueries/execute", "POST", {
            "query": sql, "fallbackBranchIds": [self.branch], "serializationFormat": "CSV"})
        query_path = "/api/v2/sqlQueries/" + parse.quote(status["queryId"], safe="")
        deadline = time.monotonic() + timeout
        while status["type"] == "running":
            if time.monotonic() > deadline:
                self.request(query_path + "/cancel", "POST")
                raise self.error_type(f"Backend history query exceeded {timeout} seconds and was canceled")
            time.sleep(1)
            status = self.request(query_path + "/getStatus", retry=retry)
        if status["type"] != "succeeded":
            raise self.error_type(f"Backend history query {status['type']}")
        content = self.request(query_path + "/getResults", retry=retry)
        # Python's default CSV field limit is 128 KiB, below a legal event's
        # 900 KB payload. This process-wide limit only increases; restoring it
        # after a parse could break another concurrently running history read.
        csv.field_size_limit(max(csv.field_size_limit(), 2_000_000))
        return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))

    def read_events(self, project=None, run_id=None, entity=None):
        """Merge durable and fresh history, remove duplicate IDs, then order events.

        Main callers: SDK resume and integration checks. Both sources are required;
        permission and query failures are surfaced instead of hiding missing history.
        """
        from backprop.event_order import ordered_events
        events = []
        for record in self.query_archive(project, run_id, entity):
            if record.get("event_type") == "workspace_snapshot":
                continue
            event = self.record_to_event(record)
            events.append(event)
        for record in self.read_live_records():
            if record.get("event_type") == "workspace_snapshot":
                continue
            if any(value is not None and record[column] != value for column, value in
                   (("project", project), ("run_id", run_id), ("entity", entity))):
                continue
            event = self.record_to_event(record)
            events.append(event)
        return ordered_events(events)

    def control_reader(self, **scope):
        """Prime a process-scoped reader before its run_start is published."""
        from .http_controls import HttpControlReader
        return HttpControlReader(self, **scope)

    def verify_credentials(self):
        """Verify read access to this installation without publishing any data."""
        self.request(self.stream_path + "/getEndOffsets?" + self._stream_query(), retry=True)
        return True

    def upload_file(self, path, name=None):
        """Upload an immutable blob and return an authenticated dataset reference.

        Main callers: SDK rich media and artifact serialization. Content hashes
        make retries safe; caller names never become directory traversal paths.
        """
        source = Path(path)
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        filename = Path(name or source.name).name
        file_path = f"blobs/{digest}/{filename}"
        dataset_rid = self.config["media_dataset_rid"]
        endpoint = f"/api/v2/datasets/{parse.quote(dataset_rid, safe='')}/files/{parse.quote(file_path, safe='')}/upload"
        endpoint += "?" + parse.urlencode({"branchName": self.branch})
        self.request(endpoint, "POST", data, "application/octet-stream", retry=True)
        return {"dataset_rid": dataset_rid, "path": file_path, "branch": self.branch,
                "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "size": len(data), "sha256": digest,
                "digest": base64.b64encode(hashlib.md5(data).digest()).decode()}

    def read_artifact_resolution(self, key):
        """Read the immutable choice shared by independently synced copies."""
        from .artifact_resolutions import read
        return read(self, key)

    def claim_artifact_resolution(self, key, candidate):
        """Save or read the first completed choice in the existing media dataset."""
        from .artifact_resolutions import claim
        return claim(self, key, candidate)

    def file_url(self, reference):
        """Return the authenticated download URL for one installation-owned blob.

        Main callers: download_file and Public Api File.url. Logged values cannot
        choose the host, dataset or branch to which a bearer token is sent.
        """
        if reference.get("dataset_rid") != self.config.get("media_dataset_rid"):
            raise ValueError("File reference belongs to a different backend dataset")
        path = reference.get("path", "")
        if not isinstance(path, str) or not re.fullmatch(r"blobs/[0-9a-f]{64}/[^/\\\\]+", path):
            raise ValueError("Invalid content-addressed Backprop file reference")
        if reference.get("sha256") is not None and reference["sha256"] != path.split("/")[1]:
            raise ValueError("File content address disagrees with its SHA-256 manifest")
        branch = reference.get("branch", self.branch)
        if branch != self.branch:
            raise ValueError("File reference belongs to a different backend branch")
        endpoint = (f"/api/v2/datasets/{parse.quote(reference['dataset_rid'], safe='')}"
                    f"/files/{parse.quote(path, safe='')}/content?")
        return self.host + endpoint + parse.urlencode({"branchName": branch})

    def download_file(self, reference):
        """Download exact bytes and verify their immutable hash before returning.

        Main callers: rich-value hydration, restore, artifact entries and Public
        Api downloads. JSON media must remain bytes, rather than being decoded by
        the ordinary API response parser. Failed integrity checks never write a
        destination file.
        """
        url = self.file_url(reference)
        data = self.request(url[len(self.host):], retry=True, raw=True)
        expected = reference.get("sha256") or reference["path"].split("/")[1]
        if hashlib.sha256(data).hexdigest() != expected:
            raise self.error_type("Downloaded file failed its SHA-256 integrity check")
        if reference.get("size") is not None and len(data) != reference["size"]:
            raise self.error_type("Downloaded file size does not match its manifest")
        return data
