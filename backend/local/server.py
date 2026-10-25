"""
Purpose: Serve the dashboard and the HTTP subset that Backprop already uses.
Input: A local data directory and a loopback socket address.
Output: A dashboard, a connection file, and durable event and artifact APIs.
Note: This server does not provide OAuth or general Foundry operations.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import shlex
import socket
import sqlite3
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from backprop_backend.assets import asset_names, asset_path
from .config import DEFAULT_PORT, MEDIA_RID, STREAM_RID, local_config
from .database import LocalConflict, LocalDatabase, _write_durable


MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 2_500_000
STREAM_PATH = f"/api/v2/highScale/streams/datasets/{STREAM_RID}/streams/master/"
CSP = "; ".join([
    "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
    "connect-src 'self' blob:", "img-src 'self' blob: data:",
    "media-src 'self' blob:", "frame-src 'self' blob:",
    "worker-src 'self' blob:", "object-src 'none'", "base-uri 'self'",
    "form-action 'none'", "frame-ancestors 'none'",
])


def create_server(directory: str | Path, *, host: str = "127.0.0.1",
                  port: int = DEFAULT_PORT,
                  write_connection: bool = True) -> ThreadingHTTPServer:
    """
    Purpose: Bind one loopback server and write its explicit connection file.
    Input: The data directory, loopback address, and TCP port.
    Output: A server that the caller must close.
    """
    authority = f"[{host}]" if ":" in host else host
    local_config(f"http://{authority}:{port or DEFAULT_PORT}")
    database = LocalDatabase(directory)
    server_class = _IPv6Server if ":" in host else ThreadingHTTPServer
    server = server_class((host, port), _Handler)
    server.daemon_threads = False
    server.database = database
    server.origin = f"http://{authority}:{server.server_port}"
    server.connection = local_config(server.origin, local_id=database.local_id)
    server.asset_names = frozenset(asset_names())
    server.asset_paths = {name: asset_path(name) for name in server.asset_names}
    if write_connection:
        _write_durable(database.directory / "connection.json",
                       (json.dumps(server.connection, indent=2) + "\n").encode())
    return server


@contextmanager
def running_server(directory: str | Path, *, host: str = "127.0.0.1",
                   port: int = 0,
                   write_connection: bool = True) -> Iterator[ThreadingHTTPServer]:
    """
    Purpose: Own the server thread and wait for active requests at shutdown.
    Input: A data directory and an optional loopback socket address.
    Output: A live server within the context manager.
    """
    server = create_server(directory, host=host, port=port,
                           write_connection=write_connection)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def serve(directory: str, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          import_paths: list[str] | None = None) -> None:
    """
    Purpose: Run the CLI dashboard until the user stops the process.
    Input: A data directory, loopback address, port, and optional journal paths.
    Output: A durable local installation and its startup instructions.
    """
    with running_server(directory, host=host, port=port) as server:
        if import_paths:
            import_journals(server, import_paths)
        config = server.database.directory / "connection.json"
        print(f"Backprop dashboard: {server.origin}/", flush=True)
        print(f"SDK connection: export BACKPROP_CONFIG={shlex.quote(str(config))}", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


def import_journals(server: ThreadingHTTPServer, paths: list[str]) -> int:
    """
    Purpose: Import complete journal prefixes through the ordinary uploader.
    Input: A live local server and journal files or parent directories.
    Output: The number of source records in all completed imports.
    """
    from .journal_import import discover_journals, import_journal
    from .store import LocalStore
    store = LocalStore(server.connection)
    count = 0
    for directory in discover_journals(paths):
        count += import_journal(directory, store)
    print(f"Backprop imported {count} journal records", flush=True)
    return count


class _IPv6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    """
    Purpose: Dispatch only Backprop stream, history, file, and static requests.
    Input: One complete HTTP request from a loopback client.
    Output: A bounded response after the database operation completes.
    """

    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        """
        Purpose: Apply the origin boundary and map storage errors to the wire.
        Input: The request headers, target, and body.
        Output: One complete HTTP response or a closed interrupted connection.
        """
        try:
            self._check_origin()
            self._route()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except FileExistsError:
            self._error(404, "FileAlreadyExists", "The immutable claim already exists")
        except FileNotFoundError:
            self._error(404, "FileNotFoundOnBranch", "The requested resource is absent")
        except LocalConflict as error:
            self._error(409, "ConflictingIdentity", str(error))
        except PermissionError as error:
            self._error(403, "InvalidOrigin", str(error))
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            self._error(400, "InvalidArgument", str(error))
        except sqlite3.OperationalError:
            self._error(503, "StorageUnavailable", "The local database is unavailable")
        except (OSError, sqlite3.DatabaseError):
            self._error(500, "StorageFailure", "The local storage operation failed")

    def _check_origin(self) -> None:
        """
        Purpose: Reject foreign origins and non-loopback Host headers.
        Input: The Host, Origin, and browser request context headers.
        Output: A validated loopback origin or a permission failure.
        """
        origin = "http://" + self.headers.get("Host", "")
        local_config(origin)
        if self.headers.get("Origin", origin) != origin:
            raise PermissionError("Local requests require the same browser origin")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("Cross-site local requests are refused")
        self.origin = origin

    def _route(self) -> None:
        """
        Purpose: Select one supported operation without a general API fallback.
        Input: The HTTP method, path, and query parameters.
        Output: A response from the selected Backprop operation.
        """
        parsed = urlsplit(self.path)
        path = unquote(parsed.path, errors="strict")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError("Duplicate query parameters are not supported")
        if path == "/config.json" and self.command in {"GET", "HEAD"}:
            self._json(local_config(self.origin, local_id=self.server.database.local_id))
        elif path.startswith(STREAM_PATH):
            self._stream(path[len(STREAM_PATH):], query)
        elif path == "/api/v2/sqlQueries/execute" and self.command == "POST":
            body = self._json_body()
            if (body.get("serializationFormat") != "CSV"
                    or body.get("fallbackBranchIds") != ["master"]):
                raise ValueError("Only the Backprop CSV history query is supported")
            identifier = self.server.database.execute_query(body["query"])
            self._json({"queryId": identifier, "type": "succeeded"})
        elif re.fullmatch(r"/api/v2/sqlQueries/[0-9a-f]{32}/(getStatus|getResults|cancel)", path):
            self._query(path)
        elif path.startswith(f"/api/v2/datasets/{MEDIA_RID}/files/"):
            self._file(path.split("/files/", 1)[1], query)
        elif re.fullmatch(r"/api/backprop/imports/[0-9a-f]{64}", path):
            self._import(path.rsplit("/", 1)[1])
        elif path.startswith("/api/"):
            raise ValueError("This endpoint is outside the Backprop local API")
        elif self.command in {"GET", "HEAD"}:
            self._static(path)
        else:
            raise ValueError("Unsupported Backprop local operation")

    def _stream(self, operation: str, query: dict) -> None:
        """
        Purpose: Preserve the existing publish and offset-based read protocol.
        Input: One stream operation and its validated parameters.
        Output: Committed uploads or records with stable decimal offsets.
        """
        database = self.server.database
        if set(query) - {"preview", "partitionId", "startOffset", "limit"}:
            raise ValueError("Unsupported Backprop stream parameter")
        if operation == "publishRecords" and self.command == "POST":
            body = self._json_body()
            if set(body) != {"records"} or not isinstance(body["records"], list):
                raise ValueError("A stream upload requires a records list")
            if len(body["records"]) > 500:
                raise ValueError("A stream upload exceeds 500 records")
            database.publish(body["records"])
            self._json({})
        elif operation == "getEndOffsets" and self.command in {"GET", "HEAD"}:
            self._json({"0": str(database.end_offset())})
        elif operation == "getRecords" and self.command in {"GET", "HEAD"}:
            if query.get("partitionId", ["0"]) != ["0"]:
                raise ValueError("The Backprop local stream has one partition")
            start = int(query.get("startOffset", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            if start < 0 or not 1 <= limit <= 1000:
                raise ValueError("Invalid stream offset or page size")
            self._json(database.read_records(start=start, limit=limit))
        else:
            raise ValueError("Unsupported Backprop stream operation")

    def _query(self, path: str) -> None:
        """
        Purpose: Serve retryable snapshots and release canceled query metadata.
        Input: A query identifier and one existing history operation.
        Output: CSV with its stream boundary, status, or idempotent cancellation.
        """
        _, identifier, operation = path.rsplit("/", 2)
        database = self.server.database
        if operation == "getResults" and self.command in {"GET", "HEAD"}:
            result, boundary = database.query_page(identifier)
            self._send(result, content_type="text/csv; charset=utf-8",
                       headers={"X-Backprop-End-Offset": str(boundary)})
        elif operation == "getStatus" and self.command in {"GET", "HEAD"}:
            if not database.has_query(identifier):
                raise FileNotFoundError("Unknown or expired Backprop history query")
            self._json({"queryId": identifier, "type": "succeeded"})
        elif operation == "cancel" and self.command == "POST":
            database.cancel_query(identifier)
            self._json({})
        else:
            raise ValueError("Unsupported Backprop history query operation")

    def _file(self, suffix: str, query: dict) -> None:
        """
        Purpose: Dispatch immutable blob reads and atomic APPEND claims.
        Input: A file path, operation, and branch query.
        Output: Verified bytes or a durable upload acknowledgement.
        """
        if query.get("branchName") != ["master"]:
            raise ValueError("The Backprop local branch is master")
        path, operation = suffix.rsplit("/", 1)
        if operation == "upload" and self.command == "POST":
            if set(query) - {"branchName", "transactionType"}:
                raise ValueError("Unsupported file upload parameter")
            transaction = query.get("transactionType")
            if transaction not in (None, ["APPEND"]):
                raise ValueError("Unsupported file transaction type")
            data = self._body(MAX_UPLOAD_BYTES, "application/octet-stream")
            self.server.database.upload_file(path, data, append=transaction == ["APPEND"])
            self._json({})
        elif operation == "content" and self.command in {"GET", "HEAD"}:
            if set(query) != {"branchName"}:
                raise ValueError("Unsupported file read parameter")
            self._send(self.server.database.read_file(path), allow_range=True)
        else:
            raise ValueError("Unsupported Backprop file operation")

    def _import(self, identifier: str) -> None:
        """
        Purpose: Validate and persist a separate local ingestion cursor.
        Input: A source identity and an optional completed prefix descriptor.
        Output: The committed cursor for this installation.
        """
        state = None
        if self.command == "POST":
            state = self._json_body()
            if (set(state) != {"count", "byte_offset", "sha256"}
                    or any(type(state[key]) is not int or state[key] < 0
                           for key in ("count", "byte_offset"))
                    or not isinstance(state["sha256"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", state["sha256"]) is None):
                raise ValueError("Invalid local import cursor")
        elif self.command not in {"GET", "HEAD"}:
            raise ValueError("Unsupported local import operation")
        self._json(self.server.database.import_state(identifier, state))

    def _static(self, path: str) -> None:
        """
        Purpose: Serve only dashboard assets, without local connection secrets.
        Input: A single asset filename from the request path.
        Output: The packaged asset or a missing-resource error.
        """
        name = "index.html" if path == "/" else path[1:]
        if name not in self.server.asset_names:
            raise FileNotFoundError("Unknown dashboard asset")
        asset = self.server.asset_paths[name]
        if not asset.is_file():
            raise FileNotFoundError("Unknown dashboard asset")
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith((".mjs", ".js")):
            content_type = "text/javascript"
        self._send(asset.read_bytes(), content_type=content_type)

    def _json_body(self) -> dict:
        body = json.loads(self._body(MAX_JSON_BYTES, "application/json"))
        if not isinstance(body, dict):
            raise ValueError("The request body must be a JSON object")
        return body

    def _body(self, limit: int, content_type: str) -> bytes:
        """
        Purpose: Reject partial uploads before any storage operation starts.
        Input: An exact content type, byte limit, and Content-Length header.
        Output: The complete body or a validation failure.
        """
        if (self.headers.get("Transfer-Encoding")
                or self.headers.get_content_type() != content_type):
            raise ValueError("Unsupported request body encoding")
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1:
            raise ValueError("One Content-Length header is required")
        length = int(values[0])
        if not 0 <= length <= limit:
            raise ValueError("The request body exceeds the local upload limit")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("The upload ended before its complete body arrived")
        return body

    def _json(self, value: object) -> None:
        self._send(json.dumps(value, allow_nan=False, separators=(",", ":")).encode(),
                   content_type="application/json")

    def _error(self, status: int, name: str, message: str) -> None:
        self._send(json.dumps({"errorCode": status, "errorName": name,
                               "message": message}).encode(),
                   status=status, content_type="application/json")

    def _send(self, data: bytes, *, status: int = 200,
              content_type: str = "application/octet-stream",
              allow_range: bool = False,
              headers: dict[str, str] | None = None) -> None:
        """
        Purpose: Return a complete response with a strict local browser policy.
        Input: Committed bytes, an HTTP status, and optional media range support.
        Output: A length-delimited response that contains no server credentials.
        """
        headers = dict(headers or {})
        requested = self.headers.get("Range") if allow_range else None
        if requested:
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", requested)
            is_valid = bool(match and any(match.groups()) and data)
            if is_valid and match[1]:
                start = int(match[1])
                end = min(int(match[2]) if match[2] else len(data) - 1, len(data) - 1)
            elif is_valid:
                start, end = max(0, len(data) - int(match[2])), len(data) - 1
            if not is_valid or start >= len(data) or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            data, status = data[start:end + 1], 206
        self.send_response(status)
        for name, value in {
            "Content-Type": content_type, "Content-Length": str(len(data)),
            "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store", "Connection": "close", **headers,
        }.items():
            self.send_header(name, value)
        if allow_range:
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(data)
