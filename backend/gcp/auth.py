"""
Purpose: Isolate Google credentials from Foundry and verify authorized identities.
Input: Google access tokens, ADC, and an operator-selected email allowlist.
Output: SDK authentication candidates or verified service identities.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from backprop_backend.auth import AuthCandidate
from .config import normalize_gcp_config


def authenticate_candidate(*, config: dict, native_store: Callable,
                           key: str | None = None, host: str | None = None,
                           verify: bool = False, timeout: float | None = None,
                           settings: Any = None, **options: Any) -> AuthCandidate:
    """
    Purpose: Select only explicit Google credentials or ADC without Foundry prompts.
    Input: The selected connection and public SDK authentication options.
    Output: A detached Google session, with optional bounded verification.
    """
    config = normalize_gcp_config(config)
    if host is not None and host != config["host"]:
        raise ValueError("Google login host must match BACKPROP_CONFIG")
    token = key or os.environ.get("BACKPROP_GCP_ACCESS_TOKEN")
    if verify:
        from backprop.runtime import bounded_call
        timeout = timeout if timeout is not None else getattr(settings, "login_timeout", None)
        if timeout:
            config["timeout"] = min(config.get("timeout", 60), timeout)
        bounded_call(lambda: native_store(config, token=token).verify_credentials(),
                     timeout, "Google login")
    return AuthCandidate(token, config["host"], "backend", backend="gcp")


class GoogleAuthorizer:
    """
    Purpose: Verify access tokens with Google before each protected request.
    Input: A nonempty email allowlist and optional token verification function.
    Output: A verified Google identity or a permission failure.
    Note: Tokens and Google response bodies never enter diagnostics or logs.
    """

    def __init__(self, allowed_emails: list[str], *, verify_token: Callable | None = None):
        self.allowed_emails = frozenset(email.lower() for email in allowed_emails)
        if not self.allowed_emails or any("@" not in email for email in self.allowed_emails):
            raise ValueError("Configure a nonempty Google email allowlist")
        self.verify_token = verify_token or _verify_access_token

    def __call__(self, authorization: str) -> dict:
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise PermissionError("A Google access token is required")
        claims = self.verify_token(authorization[7:])
        email = str(claims.get("email", "")).lower()
        if (email not in self.allowed_emails
                or claims.get("email_verified", claims.get("verified_email")) not in (True, "true")
                or int(claims.get("expires_in", 0)) <= 0):
            raise PermissionError("This Google identity is not authorized")
        return {"userId": claims.get("sub") or claims.get("user_id") or email,
                "displayName": email}


def _verify_access_token(token: str) -> dict:
    import requests
    try:
        response = requests.get("https://oauth2.googleapis.com/tokeninfo",
                                params={"access_token": token}, timeout=15)
        if response.status_code != 200:
            raise PermissionError("Google rejected the access token")
        return response.json()
    except (requests.RequestException, ValueError):
        raise PermissionError("Google authentication could not be verified") from None
