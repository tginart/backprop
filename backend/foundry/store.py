"""
Purpose: Connect the shared HTTP contract to native Foundry resources.
Input: Foundry dataset configuration and a bearer token.
Output: An authenticated HTTPS store with Foundry transaction retry rules.
"""
import json
import os
import re
import ssl
import time
from urllib import parse, request

from backprop_backend.http_store import BackendError, HttpStore, _NoRedirect


class FoundryError(BackendError):
    """Preserve the existing Foundry exception name and shared error fields."""



class FoundryStore(HttpStore):
    """
    Purpose: Apply Foundry credentials and origin checks to the shared HTTP store.
    Input: A Foundry connection dictionary and an optional token.
    Output: An authenticated native Foundry adapter.
    """
    backend_label = "Foundry"
    error_type = FoundryError
    _live_read_workers = 4
    _accept_compression = True

    def __init__(self, config: dict, token: str | None = None):
        self.config = dict(config)
        self._live_page_size = 1000
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
