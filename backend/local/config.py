"""
Purpose: Define the explicit connection boundary for a local Backprop server.
Input: A connection dictionary or a loopback HTTP origin.
Output: A validated connection with fixed Backprop resource identifiers.
Note: Foundry connections retain their HTTPS and credential requirements.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


STREAM_RID = "ri.foundry.local.dataset.events"
MEDIA_RID = "ri.foundry.local.dataset.media"
DEFAULT_PORT = 8765


def validate_local_origin(host: str) -> str:
    """
    Purpose: Permit HTTP only for an explicit loopback origin.
    Input: The proposed server origin.
    Output: The same origin, or a ValueError for an invalid origin.
    """
    if not isinstance(host, str) or any(ord(c) <= 32 for c in host):
        raise ValueError("Local host must be a loopback HTTP origin")
    try:
        parsed = urlsplit(host)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Local host has an invalid port") from error
    if (parsed.scheme != "http" or not parsed.hostname or parsed.path
            or parsed.query or parsed.fragment or parsed.username is not None
            or parsed.password is not None or port == 0):
        raise ValueError("Local host must be a loopback HTTP origin")
    if parsed.hostname != "localhost":
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError("Local HTTP connections require a loopback address")
    return host


def local_config(host: str, *, local_id: str | None = None) -> dict:
    result = {
        "backend": "local", "host": validate_local_origin(host),
        "stream_dataset_rid": STREAM_RID, "media_dataset_rid": MEDIA_RID,
        "branch": "master", "app_url": host + "/",
    }
    if local_id is not None:
        result["local_id"] = local_id
    return result


def normalize_local_config(config: dict) -> dict:
    """
    Purpose: Reject resource or branch overrides outside the local contract.
    Input: An explicit local connection dictionary.
    Output: A new dictionary with the local defaults.
    """
    if config.get("backend") != "local":
        raise ValueError("Local connections require backend='local'")
    defaults = local_config(config.get("host"))
    for name in ("stream_dataset_rid", "media_dataset_rid", "branch"):
        if config.get(name, defaults[name]) != defaults[name]:
            raise ValueError(f"The local backend does not support a custom {name}")
    if config.get("stream_view_rid"):
        raise ValueError("The local backend does not use stream views")
    return {**defaults, **config}
