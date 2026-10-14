"""Classify upload failures without parsing credential-bearing error messages.

Architecture: native transport attaches HTTP retryability -> journal wrappers
preserve the cause -> the background uploader either backs off or stops sending.
Only an unrecoverable failure activates stop_on_fatal_error; both paths retain
the local journal. Main callers: FoundryStore.request and Run background checks.
Why this exists: a failed attempt does not mean that training must stop.
"""
from __future__ import annotations

import errno
import http.client
import ssl
from urllib.error import HTTPError, URLError


def retryable_status(status):
    """Match the pinned SDK's ordinary HTTP upload retry policy.

    Main callers: native HTTP error construction and request retry loops.
    See upstream core/internal/clients/retry_policy.go, RetryMostFailures.
    Redirects stay failures because credentials must remain on the configured
    Foundry origin; no retry changes authentication or certificate validation.
    """
    permanent = {400, 401, 402, 403, 404, 409, 410, 413, 422, 501}
    return status not in permanent and (status == 0 or status >= 400)


def retryable_failure(error):
    """Follow explicit causes and identify failures another attempt can fix.

    Main callers: Run._background/_check and FoundryStore.request. Native HTTP
    metadata wins over wrapper wording. Local validation, missing staged files,
    integrity failures and unknown programming errors are not silently retried.
    """
    seen = set()
    current = error
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        declared = getattr(current, "retryable", None)
        if isinstance(declared, bool):
            return declared
        if isinstance(current, ssl.SSLCertVerificationError):
            return False
        if isinstance(current, HTTPError):
            return retryable_status(current.code)
        if isinstance(current, URLError):
            if isinstance(current.reason, BaseException):
                current = current.reason
                continue
            return True
        if isinstance(current, (ConnectionError, TimeoutError, http.client.IncompleteRead,
                                http.client.RemoteDisconnected, ssl.SSLEOFError,
                                ssl.SSLZeroReturnError)):
            return True
        if isinstance(current, OSError) and current.errno in {
            errno.EAGAIN, errno.EINTR, errno.ETIMEDOUT, errno.ECONNRESET,
            errno.ECONNABORTED, errno.ECONNREFUSED, errno.EHOSTUNREACH,
            errno.ENETUNREACH, errno.ENETDOWN, errno.EPIPE,
        }:
            return True
        if current.__cause__ is not None:
            current = current.__cause__
            continue
        # urllib wraps DNS/socket errors whose errno is not a POSIX network
        # number. They are explicitly connection failures at this boundary.
        import socket
        return isinstance(current, (socket.gaierror, socket.herror))
    return False
