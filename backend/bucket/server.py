"""
Purpose: Serve the local dashboard over a bucket installation.
Input: A gs://, s3:// or file:// URI, a loopback address and a cache directory.
Output: The local HTTP contract, backed by a DuckDB mirror of the bucket.
Note: The dashboard sees a local backend. Its stop requests and workspace
    saves are written through to the bucket.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
import shlex
import threading

from backprop_backend.local.config import DEFAULT_PORT
from backprop_backend.local.server import _Handler as LocalHandler, running_server
from .cache import ObjectCache
from .config import cache_directory, canonical_uri
from .objects import BucketError, open_object_store
from .segments import ensure_installation


DEFAULT_REFRESH_S = 15.0


@contextmanager
def running_bucket_server(uri: str, *, host: str = "127.0.0.1", port: int = 0,
                          cache_dir: str | Path | None = None,
                          refresh_s: float = DEFAULT_REFRESH_S,
                          ) -> Iterator[ThreadingHTTPServer]:
    """
    Purpose: Run a dashboard server over a bucket for the life of the context.
    Input: The installation URI, loopback address, cache directory and the
        interval between background refreshes.
    Output: A live server. The mirror and its refresher stop at exit.
    Method:
        1. Read or create the installation identity.
        2. Open the persistent mirror and ingest the current bucket contents.
        3. Start the HTTP server and the background refresher.
    """
    from .mirror import BucketMirror
    uri = canonical_uri(uri)
    objects = open_object_store(uri)
    bucket_id = ensure_installation(objects)["bucket_id"]
    directory = cache_directory(uri, cache_dir)
    mirror = BucketMirror(objects, ObjectCache(objects, directory), bucket_id=bucket_id,
                          database_path=directory / "mirror.duckdb")
    try:
        print(f"Backprop bucket: reading {uri}", flush=True)
        added = mirror.refresh()
        print(f"Backprop bucket: {mirror.end_offset()} events ({added} new)", flush=True)
        with running_server(host=host, port=port, database=mirror,
                            handler_class=_BucketHandler) as server:
            server.bucket_uri = uri
            mirror.start_refresher(refresh_s)
            yield server
    finally:
        # running_server also closes the mirror. A second close has no effect.
        mirror.close()


def serve_bucket(uri: str, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 cache_dir: str | None = None, refresh_s: float = DEFAULT_REFRESH_S,
                 import_paths: list[str] | None = None) -> None:
    """
    Purpose: Run the CLI dashboard over a bucket until the user stops it.
    Input: The installation URI, server options and optional journal paths.
    Output: A running dashboard and the direct SDK connection instructions.
    """
    from backprop_backend.local.server import import_journals
    with running_bucket_server(uri, host=host, port=port, cache_dir=cache_dir,
                               refresh_s=refresh_s) as server:
        if import_paths:
            import_journals(server, import_paths)
        print(f"Backprop dashboard: {server.origin}/", flush=True)
        print("SDK connection: export BACKPROP_BACKEND=bucket "
              f"BACKPROP_BUCKET_URI={shlex.quote(server.bucket_uri)}", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass


class _BucketHandler(LocalHandler):
    """
    Purpose: Map bucket and DuckDB failures to complete HTTP error responses.
    Input: One request for the local contract.
    Output: The local response, or a 503 or 500 storage error.
    """

    def _dispatch(self) -> None:
        import duckdb
        try:
            super()._dispatch()
        except BucketError as error:
            if error.retryable:
                self._error(503, "StorageUnavailable", "The bucket is temporarily unavailable")
            else:
                self._error(500, "StorageFailure", "The bucket operation failed")
        except duckdb.Error:
            self._error(500, "StorageFailure", "The local bucket mirror failed")
