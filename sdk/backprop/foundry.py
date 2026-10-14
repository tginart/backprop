"""Native Foundry storage for Backprop, using only Python's standard library.

Architecture: SDK events -> Foundry stream; files -> Foundry dataset. Reads merge
the stream's archived SQL view with its current offset-addressed records. Events
keep stable IDs, so overlap and at-least-once retries cannot duplicate history.
No proxy server or persistent credential file is required.
Main callers: SDK FoundrySink and backend verification tools.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import http.client
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import ssl
import time
from urllib import error, parse, request

from .errors import CommError
from .failures import retryable_failure, retryable_status


class FoundryError(CommError):
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


class FoundryStore:
    """Read and write the two resources provisioned for a Backprop installation.

    Main callers: FoundrySink. Pass a config mapping with host,
    stream_dataset_rid, media_dataset_rid, and optionally branch/stream_view_rid.
    Tokens are supplied explicitly or through BACKPROP_TOKEN / FOUNDRY_TOKEN.
    """

    _archive_page_size = 100_000

    def __init__(self, config: dict, token: str | None = None):
        self.config = dict(config)
        self._live_page_size = 100
        self.host = self.config["host"].rstrip("/")
        url = parse.urlsplit(self.host)
        if url.scheme != "https" or not url.hostname or url.path or url.query or url.fragment or url.username:
            raise ValueError("Foundry host must be an HTTPS origin without a path or credentials")
        self.token = token or os.environ.get("BACKPROP_TOKEN") or os.environ.get("FOUNDRY_TOKEN")
        if not self.token:
            raise ValueError("Set BACKPROP_TOKEN or FOUNDRY_TOKEN to a Foundry bearer token")
        self.stream_rid = self.config["stream_dataset_rid"]
        if not re.fullmatch(r"ri\.foundry\.[a-zA-Z0-9-]+\.dataset\.[a-zA-Z0-9-]+", self.stream_rid):
            raise ValueError("stream_dataset_rid is not a Foundry dataset RID")
        self.branch = self.config.get("branch", "master")
        self.stream_path = (f"/api/v2/highScale/streams/datasets/{parse.quote(self.stream_rid, safe='')}"
                            f"/streams/{parse.quote(self.branch, safe='')}")
        handlers = [_NoRedirect]
        proxies = {scheme: self.config[scheme + "_proxy"] for scheme in ("http", "https") if self.config.get(scheme + "_proxy")}
        if proxies:
            handlers.append(request.ProxyHandler(proxies))
        if self.config.get("insecure_disable_ssl"):
            handlers.append(request.HTTPSHandler(context=ssl._create_unverified_context()))
        self.opener = request.build_opener(*handlers)

    def request(self, path, method="GET", body=None, content_type="application/json", *, retry=False, raw=False):
        """Send one authenticated same-origin request and surface failures clearly.

        Main callers: publish, read_events, upload_file. Only safe reads and
        application-idempotent event/file writes opt into transient retries.
        """
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Foundry requests must use a relative API path")
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        attempts = 4 if retry else 1
        for attempt in range(attempts):
            req = request.Request(self.host + path, data=body, method=method,
                                  headers={"Authorization": "Bearer " + self.token,
                                           "Content-Type": content_type})
            try:
                with self.opener.open(req, timeout=self.config.get("timeout", 60)) as response:
                    data = response.read()
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
                        detail = exc.read(1000).decode("utf-8", errors="replace")
                        can_retry = can_retry or self._upload_transaction_conflict(path, method, exc.code, detail)
                        detail = detail.replace(self.token, "[redacted]")
                    except (OSError, http.client.HTTPException) as body_error:
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
                raise FoundryError(f"Foundry {method} failed (HTTP {exc.code}): {detail}",
                                  status_code=exc.code, retryable=can_retry, error_name=error_name) from None
            except (error.URLError, OSError, http.client.HTTPException) as exc:
                can_retry = retryable_failure(exc)
                if retry and can_retry and attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise FoundryError(f"Foundry {method} connection failed: {type(exc).__name__}",
                                  retryable=can_retry,
                                  response_truncated=isinstance(exc, http.client.IncompleteRead)) from None

    def _upload_transaction_conflict(self, path, method, status, detail):
        """Retry only contention on this installation's immutable file uploads.

        Main callers: request's HTTP-error boundary. Foundry's upload endpoint
        creates an implicit dataset transaction; another run can temporarily
        own that transaction on the shared media branch. The exact structured
        conflict is safe to retry with identical content-addressed bytes or an
        APPEND-only artifact resolution. This
        never aborts another writer, broadens retries to unknown 409 responses,
        or changes the four-attempt/backoff limit.
        """
        if status != 409 or method != "POST" or not self.config.get("media_dataset_rid"):
            return False
        endpoint = parse.urlsplit(path)
        prefix = "/api/v2/datasets/" + parse.quote(self.config["media_dataset_rid"], safe="") + "/files/"
        if not endpoint.path.startswith(prefix) or not endpoint.path.endswith("/upload"):
            return False
        query = parse.parse_qs(endpoint.query)
        normal_upload = query == {"branchName": [self.branch]}
        resolution_upload = query == {"branchName": [self.branch], "transactionType": ["APPEND"]}
        if not normal_upload and not resolution_upload:
            return False
        try:
            blob = parse.unquote(endpoint.path[len(prefix):-len("/upload")], errors="strict")
            payload = json.loads(detail)
        except (ValueError, UnicodeError):
            return False
        allowed = (normal_upload and re.fullmatch(r"blobs/[0-9a-f]{64}/[^/\\]+", blob) is not None
                   or resolution_upload and re.fullmatch(r"artifact-resolutions/[0-9a-f]{64}\.json", blob) is not None)
        return (allowed and isinstance(payload, dict)
                and payload.get("errorName") == "OpenTransactionAlreadyExists")

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

        Main callers: FoundrySink.flush. Images and large files must be uploaded
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

    def read_live_records(self):
        """Read a bounded snapshot across all stream partitions using sparse offsets.

        Main callers: read_events. Capture end offsets first so an active training
        job cannot make replay run forever. Offsets are integers encoded as strings.
        Exhausted truncated-response retries reduce page size at the same offset;
        even a one-record failure raises, so partial responses never lose data.
        """
        ends = self.request(self.stream_path + "/getEndOffsets?" + self._stream_query(), retry=True)
        for partition, end in ends.items():
            offset, end = None, int(end)
            while end > 0 and (offset is None or offset < end):
                # Rich events can approach 900 KB each. Smaller pages bound
                # response size without dropping records or changing offsets.
                params = {"partitionId": partition, "limit": self._live_page_size}
                if offset is not None:
                    params["startOffset"] = str(offset)
                try:
                    rows = self.request(self.stream_path + "/getRecords?" + self._stream_query(**params), retry=True)
                except FoundryError as exc:
                    if not exc.response_truncated or self._live_page_size == 1:
                        raise
                    self._live_page_size = max(1, self._live_page_size // 2)
                    continue
                if not rows:
                    break
                for row in rows:
                    if int(row["offset"]) < end:
                        yield row["value"]
                next_offset = max(int(row["offset"]) for row in rows) + 1
                if offset is not None and next_offset <= offset:
                    raise FoundryError("Foundry returned non-advancing stream offsets")
                offset = next_offset

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
                raise FoundryError("Foundry archive query returned a non-advancing page")
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
                raise FoundryError(f"Foundry history query exceeded {timeout} seconds and was canceled")
            time.sleep(1)
            status = self.request(query_path + "/getStatus", retry=retry)
        if status["type"] != "succeeded":
            raise FoundryError(f"Foundry history query {status['type']}")
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
        from .event_order import ordered_events
        events = []
        for record in self.query_archive(project, run_id, entity):
            if record.get("event_type") == "workspace_snapshot":
                continue  # Display revisions are not experiment or resume events.
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
        from .control_store import FoundryControlReader
        return FoundryControlReader(self, **scope)

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
        from .artifact_resolution_store import read
        return read(self, key)

    def claim_artifact_resolution(self, key, candidate):
        """Save or read the first completed choice in the existing media dataset."""
        from .artifact_resolution_store import claim
        return claim(self, key, candidate)

    def file_url(self, reference):
        """Return the authenticated download URL for one installation-owned blob.

        Main callers: download_file and Public Api File.url. Logged values cannot
        choose the host, dataset or branch to which a bearer token is sent.
        """
        if reference.get("dataset_rid") != self.config.get("media_dataset_rid"):
            raise ValueError("File reference belongs to a different Foundry dataset")
        path = reference.get("path", "")
        if not isinstance(path, str) or not re.fullmatch(r"blobs/[0-9a-f]{64}/[^/\\\\]+", path):
            raise ValueError("Invalid content-addressed Backprop file reference")
        if reference.get("sha256") is not None and reference["sha256"] != path.split("/")[1]:
            raise ValueError("File content address disagrees with its SHA-256 manifest")
        branch = reference.get("branch", self.branch)
        if branch != self.branch:
            raise ValueError("File reference belongs to a different Foundry branch")
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
            raise FoundryError("Downloaded file failed its SHA-256 integrity check")
        if reference.get("size") is not None and len(data) != reference["size"]:
            raise FoundryError("Downloaded file size does not match its manifest")
        return data
