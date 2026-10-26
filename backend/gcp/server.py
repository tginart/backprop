"""
Purpose: Serve the existing dashboard and HTTP contract on authenticated Cloud Run.
Input: Explicit Firestore settings, a private GCS bucket, and a Google allowlist.
Output: The same event, history, file, range, and control behavior as local storage.
"""
from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
import json
import os
import re
from urllib.parse import parse_qs, unquote, urlsplit

from google.api_core.exceptions import GoogleAPICallError

from backprop_backend.assets import asset_names, asset_path
from backprop_backend.local.server import _Handler as HttpHandler, CSP
from .auth import GoogleAuthorizer, GoogleVerificationUnavailable
from .config import MEDIA_RID, STREAM_RID, normalize_gcp_config
from .database import FirestoreDatabase


STREAM_PATH = f"/api/v2/highScale/streams/datasets/{STREAM_RID}/streams/master/"
GOOGLE_CSP = CSP.replace("script-src 'self'", "script-src 'self' https://accounts.google.com") \
    .replace("style-src 'self' 'unsafe-inline'", "style-src 'self' 'unsafe-inline' https://accounts.google.com") \
    .replace("connect-src 'self' blob:", "connect-src 'self' blob: https://accounts.google.com https://oauth2.googleapis.com") \
    .replace("frame-src 'self' blob:", "frame-src 'self' blob: https://accounts.google.com")


def create_server(*, database: FirestoreDatabase, origin: str, authorize: GoogleAuthorizer,
                  client_id: str = "", host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    """
    Purpose: Bind a service with durable Firestore events and GCS artifacts.
    Input: Storage, a public HTTPS origin, Google authentication, and a socket address.
    Output: A server that the caller must close.
    """
    connection = normalize_gcp_config({"backend": "gcp", "host": origin,
                                       "gcp_id": database.installation_id, "client_id": client_id})
    server = ThreadingHTTPServer((host, port), _Handler)
    server.database = database
    server.connection = connection
    server.origin = origin
    server.authorize = authorize
    server.asset_names = frozenset(asset_names())
    server.asset_paths = {name: asset_path(name) for name in server.asset_names}
    return server


class _Handler(HttpHandler):
    """
    Purpose: Reuse contract operations after Google and same-origin verification.
    Input: One request on the configured public service origin.
    Output: Public assets or an authorized storage operation.
    """

    def _check_origin(self) -> None:
        self.origin = self.server.origin
        if (self.headers.get("Origin", self.origin) != self.origin
                or self.headers.get("Sec-Fetch-Site") == "cross-site"):
            raise PermissionError("Google Cloud requests require the same browser origin")
        path = unquote(urlsplit(self.path).path, errors="strict")
        if path.startswith(("/api/", "/multipass/")):
            self.identity = self.server.authorize(self.headers.get("Authorization", ""))

    def _dispatch(self) -> None:
        try:
            super()._dispatch()
        except GoogleVerificationUnavailable:
            self._error(503, "AuthenticationUnavailable", "Google authentication is temporarily unavailable")
        except GoogleAPICallError:
            self._error(503, "StorageUnavailable", "Google Cloud storage is unavailable")

    def _route(self) -> None:
        """
        Purpose: Select the existing wire operations with GCP resource identifiers.
        Input: A validated path, method, and query.
        Output: An existing contract response without a general API fallback.
        """
        parsed = urlsplit(self.path)
        path = unquote(parsed.path, errors="strict")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError("Duplicate query parameters are not supported")
        if path == "/config.json" and self.command in {"GET", "HEAD"}:
            self._json(self.server.connection)
        elif path == "/api/backprop/identity" and self.command == "GET":
            self._json(self.identity)
        elif re.fullmatch(r"/api/backprop/uploads/[0-9a-f]{64}/([0-9]{1,2}|complete)", path) and self.command == "POST":
            _, digest, operation = path.rsplit("/", 2)
            if operation == "complete":
                self.server.database.complete_upload(digest, self._json_body())
            else:
                if int(operation) >= 64:
                    raise ValueError("The GCP upload has too many parts")
                data = self._body(8 * 1024 * 1024, "application/octet-stream")
                self.server.database.upload_part(digest, int(operation), data)
            self._json({})
        elif path.startswith(STREAM_PATH):
            self._stream(path[len(STREAM_PATH):], query)
        elif path == "/api/v2/sqlQueries/execute" and self.command == "POST":
            body = self._json_body()
            if body.get("serializationFormat") != "CSV" or body.get("fallbackBranchIds") != ["master"]:
                raise ValueError("Only the Backprop CSV history query is supported")
            identifier = self.server.database.execute_query(body["query"])
            self._json({"queryId": identifier, "type": "succeeded"})
        elif re.fullmatch(r"/api/v2/sqlQueries/[0-9a-f]{32}/(getStatus|getResults|cancel)", path):
            self._query(path)
        elif path.startswith(f"/api/v2/datasets/{MEDIA_RID}/files/"):
            self._file(path.split("/files/", 1)[1], query)
        elif path.startswith(("/api/", "/multipass/")):
            raise ValueError("This endpoint is outside the Backprop GCP API")
        elif self.command in {"GET", "HEAD"}:
            self._static(path)
        else:
            raise ValueError("Unsupported Backprop GCP operation")

    def _send(self, data: bytes, **options) -> None:
        """
        Purpose: Preserve browser policy and stream large HTTP/1 responses.
        Input: Complete response bytes and the existing response options.
        Output: A bounded response or a chunked response with correct range metadata.
        """
        headers = {**options.pop("headers", {}), "Content-Security-Policy": GOOGLE_CSP,
                   "Cross-Origin-Opener-Policy": "same-origin-allow-popups"}
        if len(data) <= 8 * 1024 * 1024:
            super()._send(data, headers=headers, **options)
            return
        status = options.get("status", 200)
        if options.get("allow_range") and self.headers.get("Range"):
            requested = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", self.headers["Range"])
            if not requested or not any(requested.groups()):
                super()._send(b"", status=416, headers={"Content-Range": f"bytes */{len(data)}"})
                return
            if requested[1]:
                start = int(requested[1])
                end = min(int(requested[2]) if requested[2] else len(data) - 1, len(data) - 1)
            else:
                start, end = max(0, len(data) - int(requested[2])), len(data) - 1
            if start >= len(data) or end < start:
                super()._send(b"", status=416, headers={"Content-Range": f"bytes */{len(data)}"})
                return
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            data, status = data[start:end + 1], 206
        self.send_response(status)
        for name, value in {
            "Content-Type": options.get("content_type", "application/octet-stream"),
            "Transfer-Encoding": "chunked", "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Connection": "close", **headers,
        }.items():
            self.send_header(name, value)
        if options.get("allow_range"):
            self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            for start in range(0, len(data), 64 * 1024):
                chunk = data[start:start + 64 * 1024]
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    def _stream(self, operation: str, query: dict) -> None:
        """
        Purpose: Preserve the shared protocol with independent Firestore partitions.
        Input: A publish, end-offset, or bounded record-page request.
        Output: Atomic publication or stable decimal cursors for each partition.
        """
        if set(query) - {"preview", "partitionId", "startOffset", "limit"}:
            raise ValueError("Unsupported Backprop stream parameter")
        if operation == "getEndOffsets" and self.command in {"GET", "HEAD"}:
            self._json(self.server.database.end_offsets())
        elif operation == "getRecords" and self.command in {"GET", "HEAD"}:
            start = int(query.get("startOffset", ["0"])[0])
            limit = int(query.get("limit", ["100"])[0])
            if start < 0 or not 1 <= limit <= 1000:
                raise ValueError("Invalid stream offset or page size")
            self._json(self.server.database.read_records(
                start=start, limit=limit, partition=query.get("partitionId", ["0"])[0],
            ))
        else:
            super()._stream(operation, query)

    def _query(self, path: str) -> None:
        """
        Purpose: Stream cached CSV results with their complete partition boundary vector.
        Input: A valid history result ID or an existing status/cancel operation.
        Output: A bounded-memory HTTP response that survives service restarts.
        """
        _, identifier, operation = path.rsplit("/", 2)
        if operation != "getResults" or self.command not in {"GET", "HEAD"}:
            super()._query(path)
            return
        blob, boundaries = self.server.database.query_blob(identifier)
        with blob.open("rb") as source:
            first = source.read(64 * 1024)
            self.send_response(200)
            for name, value in {
                "Content-Type": "text/csv; charset=utf-8",
                "Transfer-Encoding": "chunked", "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff", "Connection": "close",
                "Content-Security-Policy": GOOGLE_CSP,
                "X-Backprop-End-Offsets": json.dumps(boundaries, separators=(",", ":")),
            }.items():
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                chunk = first
                while chunk:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    chunk = source.read(64 * 1024)
                self.wfile.write(b"0\r\n\r\n")


def main() -> None:
    """
    Purpose: Start the cloud service from explicit environment or CLI settings.
    Input: A Firestore database, bucket, origin, allowlist, and optional OAuth client.
    Output: A service with durable Firestore records and GCS artifacts.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("BACKPROP_GCP_PROJECT"), help="Explicit Google project")
    parser.add_argument("--database", default=os.environ.get("BACKPROP_FIRESTORE_DATABASE"), help="Explicit Firestore database ID")
    parser.add_argument("--bucket", default=os.environ.get("BACKPROP_GCS_BUCKET"), help="Private GCS bucket")
    parser.add_argument("--prefix", default=os.environ.get("BACKPROP_GCS_PREFIX", "backprop"), help="Isolated object prefix")
    parser.add_argument("--origin", default=os.environ.get("BACKPROP_GCP_ORIGIN"), help="Public HTTPS service origin")
    parser.add_argument("--client-id", default=os.environ.get("BACKPROP_GOOGLE_CLIENT_ID", ""), help="Google browser OAuth client ID")
    parser.add_argument("--allowed-emails", default=os.environ.get("BACKPROP_GOOGLE_ALLOWED_EMAILS", ""), help="Comma-separated Google user or service account emails")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")), help="Cloud Run HTTP port")
    args = parser.parse_args()
    if not all((args.project, args.database, args.bucket, args.origin)):
        parser.error("--project, --database, --bucket, and --origin are required")
    authorize = GoogleAuthorizer([value.strip() for value in args.allowed_emails.split(",") if value.strip()])
    from google.cloud import firestore, storage
    database = FirestoreDatabase(
        firestore.Client(project=args.project, database=args.database),
        storage.Client(project=args.project).bucket(args.bucket), prefix=args.prefix,
    )
    server = create_server(database=database, origin=args.origin, authorize=authorize,
                           client_id=args.client_id, port=args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
