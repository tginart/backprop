"""
Purpose: Validate bucket connections and derive their local cache location.
Input: A connection dictionary or a gs://, s3:// or file:// URI.
Output: A detached connection that reuses the local dashboard resource IDs.
Note: This module imports no cloud SDK, so the CLI can check URIs cheaply.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from backprop_backend.local.config import DEFAULT_PORT, MEDIA_RID, STREAM_RID


SCHEMES = ("gs", "s3", "file")
DEFAULT_APP_URL = f"http://127.0.0.1:{DEFAULT_PORT}/"
_BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")


def is_bucket_uri(value: str) -> bool:
    return isinstance(value, str) and value.split("://", 1)[0] in SCHEMES and "://" in value


def parse_bucket_uri(uri: str) -> tuple[str, str, str]:
    """
    Purpose: Split one installation URI into its scheme, bucket and key prefix.
    Input: A gs://bucket/prefix, s3://bucket/prefix or file:///path URI.
    Output: The scheme, the bucket name (empty for file://) and the prefix.
    Note: The prefix has no leading or trailing slash. It can be empty.
    """
    if not isinstance(uri, str) or any(ord(char) <= 32 for char in uri):
        raise ValueError("Bucket URI must be a gs://, s3:// or file:// URI")
    parsed = urlsplit(uri)
    if parsed.scheme not in SCHEMES or parsed.query or parsed.fragment:
        raise ValueError("Bucket URI must be a gs://, s3:// or file:// URI")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Bucket URI must not contain credentials")
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost") or not parsed.path.startswith("/"):
            raise ValueError("A file:// bucket URI needs an absolute path")
        return "file", "", unquote(parsed.path).rstrip("/") or "/"
    if not _BUCKET_NAME.fullmatch(parsed.netloc or ""):
        raise ValueError("Bucket URI has an invalid bucket name")
    prefix = parsed.path.strip("/")
    if any(part in ("", ".", "..") for part in prefix.split("/")) and prefix:
        raise ValueError("Bucket URI prefix must not contain empty, . or .. segments")
    return parsed.scheme, parsed.netloc, prefix


def canonical_uri(uri: str) -> str:
    scheme, bucket, prefix = parse_bucket_uri(uri)
    if scheme == "file":
        return "file://" + prefix
    return f"{scheme}://{bucket}/{prefix}".rstrip("/")


def normalize_bucket_config(config: dict) -> dict:
    """
    Purpose: Reject resource overrides and fill in the fixed bucket defaults.
    Input: A connection with backend='bucket' and a uri.
    Output: A new dictionary. The host is always the canonical URI.
    Note: The host field exists only because the Public Api requires one.
        The store reads the uri, so a BACKPROP_HOST override has no effect.
    """
    if config.get("backend") != "bucket":
        raise ValueError("Bucket connections require backend='bucket'")
    if not config.get("uri"):
        raise ValueError("Bucket connections require a uri (or BACKPROP_BUCKET_URI)")
    uri = canonical_uri(config["uri"])
    defaults = {"stream_dataset_rid": STREAM_RID, "media_dataset_rid": MEDIA_RID,
                "branch": "master", "app_url": DEFAULT_APP_URL}
    for name in ("stream_dataset_rid", "media_dataset_rid", "branch"):
        if config.get(name, defaults[name]) != defaults[name]:
            raise ValueError(f"The bucket backend does not support a custom {name}")
    if config.get("stream_view_rid"):
        raise ValueError("The bucket backend does not use stream views")
    identifier = config.get("bucket_id")
    if identifier is not None and not re.fullmatch(r"[0-9a-f]{32}", str(identifier)):
        raise ValueError("bucket_id must be the installation's 32-hex identifier")
    return {**defaults, **config, "uri": uri, "host": uri}


def cache_directory(uri: str, override: str | Path | None = None) -> Path:
    """
    Purpose: Select the per-installation cache shared by all local processes.
    Input: The installation URI and an optional explicit directory.
    Output: The override, or a directory under the user cache root.
    """
    if override:
        return Path(override).expanduser().resolve()
    if os.environ.get("BACKPROP_BUCKET_CACHE_DIR"):
        return Path(os.environ["BACKPROP_BUCKET_CACHE_DIR"]).expanduser().resolve()
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    digest = hashlib.sha256(canonical_uri(uri).encode()).hexdigest()[:16]
    return root / "backprop" / "bucket" / digest
