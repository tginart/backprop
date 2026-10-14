"""Local boundaries used by the pinned external-reference algorithms.

Architecture: lazy provider imports use existing user credentials; each operation
gets a private temporary download cache; handler output is read before cleanup.
No provider bytes or credentials are sent to Foundry. An immutable manifest owns
only the URI and provider integrity/version metadata. Main callers: the adapted
handler modules and external_references. Why this exists: the upstream handlers
are portable, but the W&B artifact cache and service imports are not required.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import importlib
import logging
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..artifacts import ArtifactManifestEntry
from ..artifact_entry_fields import LogicalPath
from ..errors import Error, CommError

DEFAULT_MAX_OBJECTS = 10_000_000
ETag = URIStr = FilePathStr = str
logger = logging.getLogger(__name__)


def termlog(message, **kwargs):
    import backprop
    return backprop.termlog(message, **kwargs)


def termwarn(message, **kwargs):
    import backprop
    return backprop.termwarn(message, **kwargs)


def get_module(name, required=None, lazy=False):
    """Import only when the handler needs its provider, without installing it."""
    try:
        return importlib.import_module(name)
    except Exception as error:
        if required:
            raise Error(required) from error
        raise


util = SimpleNamespace(get_module=get_module)


def ensureprefix(value, prefix):
    return value if value.startswith(prefix) else prefix + value


def md5_string(value):
    return base64.b64encode(hashlib.md5(value.encode(), usedforsecurity=False).digest()).decode()


def md5_file_b64(path):
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode()


def local_file_uri_to_path(uri):
    """Retain pinned URI decoding, including host/fragment path treatment."""
    path = urlparse(uri).path if uri.startswith('file:') else uri
    return url2pathname(path)


class StorageHandler:
    """Marker base; concrete pinned handlers implement the complete dispatch."""


class ReferenceCache:
    """Per-operation scratch, never a persistent cache of mutable provider data.

    Main callers: load_path in pinned handlers. Cache hits deliberately remain
    false, so separate downloads recheck original provider metadata. The caller
    closes the cache after reading its result, including on handler failure.
    """
    def __init__(self):
        self._temporary = None

    def close(self):
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def _path(self):
        if self._temporary is None:
            self._temporary = tempfile.TemporaryDirectory(prefix='backprop-reference-')
        path = str(Path(self._temporary.name) / 'content')
        @contextmanager
        def cache_open(mode='wb'):
            with open(path, mode) as stream:
                yield stream
        return path, False, cache_open

    def check_digest_obj_path(self, digest, size):
        return self._path()

    def check_etag_obj_path(self, url, etag, size):
        return self._path()


def get_artifact_file_cache():
    return ReferenceCache()


class HTTPSession:
    """Use the pinned retry/status boundary around optional HTTP references.

    Main caller: external_references._handler. Construction stays lazy so
    checksum=False requires neither requests nor a network call. Each handler
    owns its session and closes it after metadata capture or file download.
    Failed responses never become artifact manifests or downloaded file bytes.
    """
    def __init__(self):
        self._session = None

    def get(self, url, stream=True):
        if self._session is None:
            requests = get_module('requests', required='HTTP references require the requests library')
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry = Retry(backoff_factor=1, total=16,
                          status_forcelist=(308, 408, 409, 429, 500, 502, 503, 504))
            session = requests.Session()
            adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=64)
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            self._session = session
        response = self._session.get(url, stream=stream, timeout=60)
        try:
            response.raise_for_status()
        except Exception:
            response.close()
            raise
        return response

    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None


class TimedIf:
    """Retain handler progress output without a service or background worker."""
    def __init__(self, enabled=True):
        self.enabled, self.start = enabled, None

    def __enter__(self):
        if self.enabled:
            self.start = time.monotonic()
        return self

    def __exit__(self, *exception):
        if self.start is not None:
            termlog(f'Done. {time.monotonic() - self.start:.1f}s', prefix=False)
