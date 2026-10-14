"""Preserve the W&B public API error boundary without a service dependency.

Architecture: decorate only the pinned public methods -> pass existing SDK
errors unchanged -> format already-loaded requests HTTP errors -> normalize
ordinary exceptions unless WANDB_DEBUG requests the original exception.
Main callers: read-only public Api/Run/File/artifact/project methods. Decorators
wrap the call itself, not later iteration of returned collections or generators.
Why this exists: callers can inspect CommError.message/ exc and choose debug
tracebacks. Native Foundry errors already inherit Error. W&B service-protocol
exceptions have no native transport equivalent and no special response parser.
No optional HTTP package is imported merely to normalize a local Python error.
"""
from __future__ import annotations

import ast
from functools import wraps
import os
import sys

from ..errors import CommError, Error


def _debug():
    value = os.environ.get("WANDB_DEBUG")
    return isinstance(value, str) and value.lower() in {"y", "yes", "t", "true", "on", "1"}


def _backend_messages(response, requests):
    """Read the pinned error/errors response shapes from an actual HTTP failure."""
    try:
        data = response.json()
    except requests.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    def message(value):
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("message"), str) and value["message"]:
            return value["message"]
        return None
    if error := data.get("error"):
        found = message(error)
        return [found] if found else []
    if isinstance(data.get("errors"), list) and data["errors"]:
        return [found for item in data["errors"] if (found := message(item))]
    return []


def _http_message(error, requests):
    messages = _backend_messages(error.response, requests)
    status = error.response.status_code
    if messages:
        return f"HTTP {status}: {'; '.join(messages)}"
    if error.response.text:
        return f"HTTP {status}: {error.response.text}"
    if error.response.reason:
        return f"HTTP {status} ({error.response.reason})"
    return f"HTTP {status}"


def normalize_exceptions(function):
    """Match the pinned ordinary/HTTP exception and debug behavior per call.

    Main callers: the explicitly decorated methods in apis.public. Only ordinary
    Exception subclasses are caught, preserving KeyboardInterrupt/SystemExit.
    Literal dictionary error payloads use the pinned parser and error behavior.
    """
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as error:
            requests = sys.modules.get("requests")
            http_error = getattr(requests, "HTTPError", None)
            if isinstance(http_error, type) and isinstance(error, http_error):
                raise CommError(_http_message(error, requests), error)
            if isinstance(error, Error):
                raise
            payload = error.args[0] if error.args else error
            message = ast.literal_eval(str(payload))["message"] if str(payload).startswith("{") else str(error)
            if _debug():
                raise
            raise CommError(message, error).with_traceback(error.__traceback__)
    return wrapped
