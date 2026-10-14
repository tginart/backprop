"""Optional hosted-notebook export adapters with explicit thread ownership.

Architecture: detect an installed provider in a notebook -> fetch its current
notebook -> validate/redact bytes -> return to the capture caller for staging.
Colab's message API must run on the caller/main thread and owns its 5-second
request timeout. Kaggle has no timeout argument, so a read-only bounded worker
isolates that call. Neither adapter writes files or mutates Run state; late
results are discarded before the existing capture path can stage anything.
Main callers: notebook discovery and NotebookCapture.refresh.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import PurePosixPath
import sys
import threading
import time
from urllib.parse import unquote

from .notebook_snapshot import MAX_NOTEBOOK_BYTES, CaptureLimitError, _encode
from .runtime import bounded_call

COLAB_TIMEOUT = 5.0


def optional_module(name):
    """Import an available provider without installing anything or guessing APIs."""
    if name in sys.modules:
        return sys.modules[name]
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    return importlib.import_module(name) if spec is not None else None


def provider_name():
    if optional_module('google.colab') is not None:
        return 'colab'
    if optional_module('kaggle_session') is not None:
        return 'kaggle'
    return None


def export_name(value, fallback):
    """Constrain provider display names to one safe staged notebook filename."""
    name = PurePosixPath(str(value or fallback).replace('\\', '/')).name
    if name in {'', '.', '..'}:
        name = fallback
    return name if name.endswith('.ipynb') else name + '.ipynb'


def colab_url(settings):
    """Expose the pinned fileId notebook URL without treating it as a disk path."""
    path = getattr(settings, '_notebook_jupyter_path', None)
    if 'google.colab' in sys.modules and isinstance(path, str) and path.startswith('fileId='):
        return 'https://colab.research.google.com/notebook#' + unquote(path)
    return None


def _kaggle_notebook(module, deadline):
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Notebook provider capture deadline expired')
    source = bounded_call(lambda: module.UserSessionClient().get_exportable_ipynb()['source'], remaining, 'Kaggle notebook export')
    if not isinstance(source, str):
        raise TypeError('Kaggle notebook source must be JSON text')
    if len(source.encode('utf-8')) > MAX_NOTEBOOK_BYTES:
        raise CaptureLimitError('Kaggle notebook exceeds capture size limit')
    notebook = json.loads(source)
    if isinstance(notebook, dict):
        notebook.setdefault('metadata', {})['name'] = 'kaggle.ipynb'
    return notebook


def export_notebook(settings, redactor, deadline):
    """Return one sanitized provider snapshot, or None when no content exists.

    Main callers: NotebookCapture.refresh on the caller thread. The external
    APIs are pinned to W&B0.30 jupyter.py:297-319 and _save_ipynb:389-420.
    Provider exceptions propagate to the existing sanitized warning boundary.
    """
    provider = getattr(settings, '_notebook_provider', None)
    if provider is None:
        return None
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Notebook provider capture deadline expired')
    if provider == 'colab':
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('Colab notebook export requires the main thread')
        module = optional_module('google.colab')
        if module is None:
            raise ImportError('Colab notebook provider is unavailable')
        response = module._message.blocking_request('get_ipynb', timeout_sec=min(COLAB_TIMEOUT, remaining))
        notebook = response['ipynb'] if response else None
        name = export_name(getattr(settings, '_notebook_export_name', None), 'colab.ipynb')
        if not notebook:
            fallback = optional_module('kaggle_session')
            if fallback is None:
                return None
            provider, name = 'kaggle', 'kaggle.ipynb'
            notebook = _kaggle_notebook(fallback, deadline)
    elif provider == 'kaggle':
        module = optional_module('kaggle_session')
        if module is None:
            raise ImportError('Kaggle notebook provider is unavailable')
        notebook = _kaggle_notebook(module, deadline)
        name = 'kaggle.ipynb'
    else:
        raise ValueError('Unknown notebook provider')
    if time.monotonic() >= deadline:
        raise TimeoutError('Notebook provider returned after its capture deadline')
    if not isinstance(notebook, dict) or notebook.get('nbformat') != 4 or not isinstance(notebook.get('cells'), list):
        raise ValueError('Notebook provider did not return a version4 notebook')
    if provider == 'kaggle' and not notebook['cells']:
        return None
    data = _encode(redactor.value(notebook))
    if time.monotonic() >= deadline:
        raise TimeoutError('Notebook provider encoding exceeded its capture deadline')
    return name, data
