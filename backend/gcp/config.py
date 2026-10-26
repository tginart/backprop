"""
Purpose: Validate public GCP connections without credentials or project defaults.
Input: An HTTPS service origin and a stable installation identity.
Output: A detached connection for the shared HTTP contract.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


STREAM_RID = "ri.foundry.gcp.dataset.events"
MEDIA_RID = "ri.foundry.gcp.dataset.media"


def normalize_gcp_config(config: dict) -> dict:
    """
    Purpose: Reject unsafe origins and incompatible resource overrides.
    Input: An explicit GCP connection.
    Output: A complete public connection or a validation error.
    """
    host = config.get("host", "")
    if not isinstance(host, str) or any(ord(char) <= 32 for char in host):
        raise ValueError("GCP host must be an HTTPS origin")
    parsed = urlsplit(host)
    if (config.get("backend") != "gcp" or parsed.scheme != "https"
            or not parsed.hostname or parsed.netloc != parsed.hostname
            or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("GCP host must be an HTTPS origin without credentials")
    if not re.fullmatch(r"[0-9a-f]{32}", config.get("gcp_id", "")):
        raise ValueError("GCP connections require the installation's gcp_id")
    defaults = {"stream_dataset_rid": STREAM_RID, "media_dataset_rid": MEDIA_RID,
                "branch": "master", "app_url": host + "/"}
    for field in ("stream_dataset_rid", "media_dataset_rid", "branch"):
        if config.get(field, defaults[field]) != defaults[field]:
            raise ValueError(f"The GCP backend does not support a custom {field}")
    if config.get("stream_view_rid"):
        raise ValueError("The GCP backend does not use stream views")
    return {**defaults, **config}
