"""
Purpose: Connect the shared SDK HTTP transport to a Google-authenticated service.
Input: An explicit GCP connection and Google credentials.
Output: The existing event, history, file, retry, and artifact claim methods.
"""
from __future__ import annotations

import os
import hashlib
from urllib import parse, request

from backprop_backend.http_store import HttpStore, _NoRedirect
from .config import normalize_gcp_config


CHUNK_BYTES = 8 * 1024 * 1024


class GcpStore(HttpStore):
    """
    Purpose: Refresh ADC credentials before requests without a global token cache.
    Input: A GCP connection and an optional explicit Google access token.
    Output: An HTTPS store with an installation-specific acknowledgement namespace.
    """

    backend_label = "Google Cloud Backprop"

    def __init__(self, config: dict, token: str | None = None) -> None:
        """
        Purpose: Validate the service and namespace durable journals by installation.
        Input: A public connection and an optional Google access token.
        Output: An HTTPS adapter after a remote installation identity check.
        """
        self.config = normalize_gcp_config(config)
        self.host = self.config["host"]
        self._explicit_token = token or os.environ.get("BACKPROP_GCP_ACCESS_TOKEN")
        self._credentials = None
        self._live_page_size = 1000
        self.stream_rid = self.config["stream_dataset_rid"]
        self.branch = self.config["branch"]
        self.stream_path = (
            f"/api/v2/highScale/streams/datasets/{self.stream_rid}/streams/master"
        )
        self.ack_namespace = "gcp-" + self.config["gcp_id"]
        self.opener = request.build_opener(_NoRedirect)
        remote = self.request("/config.json")
        if (remote.get("backend") != "gcp"
                or remote.get("gcp_id") != self.config["gcp_id"]):
            raise ValueError("GCP installation changed; download its current connection")

    @property
    def token(self) -> str:
        """
        Purpose: Return an explicit Google token or refresh Application Default Credentials.
        Input: The adapter's private credential state.
        Output: A current access token without a credential-file change.
        """
        if self._explicit_token:
            return self._explicit_token
        import google.auth
        from google.auth.transport.requests import Request
        if self._credentials is None:
            self._credentials, _ = google.auth.default(scopes=[
                "https://www.googleapis.com/auth/cloud-platform",
                "https://www.googleapis.com/auth/userinfo.email",
            ])
        if not self._credentials.valid:
            self._credentials.refresh(Request())
        return self._credentials.token

    def read_events(self, project: str | None = None, run_id: str | None = None,
                    entity: str | None = None) -> list[dict]:
        from backprop.event_order import ordered_events
        return ordered_events([
            self.record_to_event(row)
            for row in self.query_archive(project, run_id, entity)
            if row.get("event_type") != "workspace_snapshot"
        ])

    def request(self, path: str, method: str = "GET", body=None,
                content_type: str = "application/json", **options):
        """
        Purpose: Preserve large file uploads below Cloud Run's HTTP/1 request limit.
        Input: An existing contract request with optional immutable artifact bytes.
        Output: The ordinary response after bounded, idempotent chunk uploads.
        """
        if (method != "POST" or content_type != "application/octet-stream"
                or not isinstance(body, bytes) or len(body) <= CHUNK_BYTES):
            return super().request(path, method, body, content_type, **options)
        endpoint = parse.urlsplit(path)
        prefix = f"/api/v2/datasets/{self.config['media_dataset_rid']}/files/"
        query = parse.parse_qs(endpoint.query)
        if (not endpoint.path.startswith(prefix) or not endpoint.path.endswith("/upload")
                or query.get("branchName") != ["master"]
                or set(query) - {"branchName", "transactionType"}
                or query.get("transactionType") not in (None, ["APPEND"])):
            raise ValueError("Large GCP uploads require an immutable Backprop file request")
        if len(body) > 512 * 1024 * 1024:
            raise ValueError("The file exceeds the 512 MiB upload limit")
        digest = hashlib.sha256(body).hexdigest()
        upload = f"/api/backprop/uploads/{digest}"
        for index, start in enumerate(range(0, len(body), CHUNK_BYTES)):
            super().request(f"{upload}/{index}", "POST", body[start:start + CHUNK_BYTES],
                            "application/octet-stream", retry=True)
        return super().request(upload + "/complete", "POST", {
            "path": parse.unquote(endpoint.path[len(prefix):-len('/upload')]),
            "size_bytes": len(body), "part_count": (len(body) + CHUNK_BYTES - 1) // CHUNK_BYTES,
            "append": query.get("transactionType") == ["APPEND"],
        }, **options)
