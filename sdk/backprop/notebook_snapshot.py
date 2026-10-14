"""Convert saved notebooks and execution history into sanitized notebook bytes.

Architecture: read-only provider/filesystem snapshot -> bounded notebook schema
construction -> credential redaction -> JSON bytes. This module never writes or
touches Run state, so timed-out snapshot workers cannot publish late data.
Main callers: NotebookCapture refresh and finish callbacks.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

MAX_NOTEBOOK_BYTES = 16 * 1024 * 1024
MAX_HISTORY_CELLS = 10_000
_CREDENTIAL_NAME = r"[\w.-]*(?:token|api[_-]?key|secret|secret[_-]access[_-]key|password|passwd|credentials?|authorization)"


class CaptureLimitError(ValueError):
    """A snapshot cannot be complete within the explicit capture budget."""


class NotebookRedactor:
    """Remove known authentication values and recognizable credential literals.

    Main callers: notebook snapshots and captured display values. This is not a
    classifier for arbitrary private information; it prevents SDK/environment
    credentials and conventional credential assignments from being copied.
    """
    def __init__(self, run):
        self.refresh(run)

    def refresh(self, run):
        """Include credentials introduced by cells after the run was started."""
        values = [run._settings.api_key, getattr(run._sink, "token", None)]
        values.extend((run._settings.azure_account_url_to_access_key or {}).values())
        values.extend(value for name, value in os.environ.items()
                      if re.search(r"(?:TOKEN|API_?KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)", name, re.I))
        self.secrets = sorted({value for value in values if isinstance(value, str) and value}, key=len, reverse=True)

    def text(self, value):
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        # Preserve executable source structure while replacing quoted values.
        value = re.sub(r"(?im)(\b" + _CREDENTIAL_NAME + r"(?:['\"]\]?)?\s*[:=]\s*)(['\"])([^\n]*?)\2",
                       lambda match: match[1] + match[2] + "[redacted]" + match[2], value)
        value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer|token)\s+)\S+", r"\1[redacted]", value)
        value = re.sub(r"(?i)([?&](?:access_token|token|api_key|key|password|secret)=)[^\s&#'\"]+", r"\1[redacted]", value)
        return value

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): "[redacted]" if re.fullmatch(_CREDENTIAL_NAME, str(key), re.I) else self.value(item)
                    for key, item in value.items()}
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise TypeError("Notebook output contains an unsupported JSON value")


def _encode(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_NOTEBOOK_BYTES:
        raise CaptureLimitError("Notebook snapshot exceeds the capture size limit")
    return data


def saved_notebook(path, redactor):
    """Read and sanitize one saved notebook without changing its source file."""
    with Path(path).open("rb") as source:
        data = source.read(MAX_NOTEBOOK_BYTES + 1)
    if len(data) > MAX_NOTEBOOK_BYTES:
        raise CaptureLimitError("Saved notebook exceeds the capture size limit")
    notebook = json.loads(data)
    if not isinstance(notebook, dict) or notebook.get("nbformat") != 4 or not isinstance(notebook.get("cells"), list):
        raise ValueError("Saved file is not a version 4 notebook")
    return _encode(redactor.value(notebook))


def execution_history(shell, outputs, redactor, deadline):
    """Build the current session's executed cells and textual/display outputs.

    Main callers: NotebookCapture.finish. This follows the pinned SDK's current
    history range, minimum-two-cell rule and stdout/display_data representation.
    Limits fail visibly rather than silently truncating the user's notebook.
    """
    manager = getattr(shell, "history_manager", None)
    if manager is None:
        return None
    cells, size = [], 0
    for session, count, execution in manager.get_range(output=True):
        if time.monotonic() >= deadline:
            raise TimeoutError("Notebook history capture deadline reached")
        if len(cells) >= MAX_HISTORY_CELLS:
            raise CaptureLimitError("Notebook execution history exceeds the cell limit")
        source, text = execution
        if not isinstance(source, str) or text is not None and not isinstance(text, str):
            raise TypeError("Notebook execution history must contain text")
        cell_outputs = [{"output_type": "stream", "name": "stdout", "text": text}] if text else []
        cell_outputs.extend({"output_type": "display_data", **item} for item in outputs.get(count, []))
        cell = redactor.value({"cell_type": "code", "execution_count": count, "metadata": {},
                              "source": source, "outputs": cell_outputs,
                              "id": hashlib.sha256(f"{session}:{count}:{source}".encode()).hexdigest()[:16]})
        size += len(_encode(cell))
        if size > MAX_NOTEBOOK_BYTES:
            raise CaptureLimitError("Notebook execution history exceeds the capture size limit")
        cells.append(cell)
    if len(cells) <= 1:
        return None
    language = getattr(getattr(shell, "kernel", None), "language_info", None) or {"name": "python", "version": sys.version}
    return _encode({"nbformat": 4, "nbformat_minor": 5, "cells": cells,
                    "metadata": redactor.value({"kernelspec": {"display_name": f"Python {sys.version_info.major}",
                        "name": f"python{sys.version_info.major}", "language": "python"}, "language_info": language})})
