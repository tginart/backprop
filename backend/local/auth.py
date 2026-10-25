"""
Purpose: Connect to a local backend without credentials or a credential cache.
Input: An explicit local connection and the SDK authentication options.
Output: A local session after optional read-only verification.
"""
from typing import Any, Callable

from backprop_backend.auth import AuthCandidate
from .config import normalize_local_config


def authenticate_candidate(*, config: dict, native_store: Callable,
                           host: str | None = None, verify: bool = False,
                           timeout: float | None = None,
                           settings: Any = None,
                           **options: Any) -> AuthCandidate:
    """
    Purpose: Validate the local origin without credential access or mutation.
    Input: A local connection, optional matching host, and verification choice.
    Output: A credential-free session scoped to that local connection.
    """
    config = normalize_local_config(config)
    if host is not None and host != config["host"]:
        raise ValueError("Local login host must match BACKPROP_CONFIG")
    if timeout is None and settings is not None:
        timeout = settings.login_timeout
    if verify:
        from backprop.runtime import bounded_call
        if timeout:
            config["timeout"] = min(config.get("timeout", 60), timeout)
        bounded_call(lambda: native_store(config).verify_credentials(), timeout, "login")
    return AuthCandidate(None, config["host"], "backend", backend="local")
