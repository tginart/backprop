"""
Purpose: Select a bucket connection without Backprop credentials.
Input: An explicit bucket connection and the SDK authentication options.
Output: A credential-free session after optional read-only verification.
Note: The cloud SDKs find their own credentials (ADC or the boto3 chain).
"""
from typing import Any, Callable

from backprop_backend.auth import AuthCandidate
from .config import normalize_bucket_config


def authenticate_candidate(*, config: dict, native_store: Callable,
                           verify: bool = False, timeout: float | None = None,
                           settings: Any = None, **options: Any) -> AuthCandidate:
    """
    Purpose: Validate the bucket URI and optionally check read access.
    Input: A bucket connection and the SDK verification choice.
    Output: A session whose host is the bucket URI.
    Note: There is no host check. The store reads only the uri field.
    """
    config = normalize_bucket_config(config)
    if timeout is None and settings is not None:
        timeout = settings.login_timeout
    if verify:
        from backprop.runtime import bounded_call
        bounded_call(lambda: native_store(config).verify_credentials(), timeout, "login")
    return AuthCandidate(None, config["uri"], "backend", backend="bucket")
