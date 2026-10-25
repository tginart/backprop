"""
Purpose: Reuse the Backprop HTTP contract without Foundry credentials.
Input: An explicit local connection dictionary.
Output: A store with the existing event, file, history, and retry methods.
Note: Local requests bypass system proxies and never transmit bearer tokens.
"""
from __future__ import annotations

import re
from urllib import parse, request

from backprop_backend.http_store import HttpStore, _NoRedirect
from .config import normalize_local_config


class LocalStore(HttpStore):
    """
    Purpose: Connect the existing transport methods to one local installation.
    Input: A local connection dictionary.
    Output: An HTTP store with a separate journal acknowledgement namespace.
    """

    backend_label = "Local Backprop"

    def __init__(self, config: dict, token: str | None = None) -> None:
        """
        Purpose: Verify the local installation before a journal cursor is used.
        Input: A local connection and an ignored compatibility token argument.
        Output: A credential-free transport with an installation-specific cursor.
        """
        self.config = normalize_local_config(config)
        self.host = self.config["host"]
        self.token = None
        self._live_page_size = 100
        self.stream_rid = self.config["stream_dataset_rid"]
        self.branch = self.config["branch"]
        self.stream_path = (
            "/api/v2/highScale/streams/datasets/"
            + parse.quote(self.stream_rid, safe="") + "/streams/master"
        )
        self.opener = request.build_opener(_NoRedirect, request.ProxyHandler({}))
        remote = self.request("/config.json")
        identifier = remote.get("local_id")
        if (remote.get("backend") != "local" or not isinstance(identifier, str)
                or re.fullmatch(r"[0-9a-f]{32}", identifier) is None):
            raise ValueError("The server is not a local Backprop installation")
        if self.config.get("local_id", identifier) != identifier:
            raise ValueError("Local installation changed; use its current connection file")
        self.config["local_id"] = identifier
        self.ack_namespace = "local-" + identifier

    def read_events(self, project: str | None = None, run_id: str | None = None,
                    entity: str | None = None) -> list[dict]:
        """
        Purpose: Read the scoped local archive without a lifetime stream scan.
        Input: Optional project, run, and entity filters.
        Output: Ordered experiment events from the synchronously committed archive.
        Note: Local archive reads include all committed events without a live tail.
        """
        from backprop.event_order import ordered_events
        events = [self.record_to_event(row) for row in
                  self.query_archive(project, run_id, entity)
                  if row.get("event_type") != "workspace_snapshot"]
        return ordered_events(events)

    def import_journal(self, directory: str) -> int:
        from backprop_backend.local.journal_import import import_journal
        return import_journal(directory, self)
