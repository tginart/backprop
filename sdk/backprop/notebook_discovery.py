"""Bounded discovery of the current saved Jupyter or VSCode notebook.

Architecture: explicit notebook_name wins -> VSCode kernel namespace -> current
kernel ID matched against Jupyter server sessions -> existing local notebook.
Only the caller commits a successful result to Settings, so a timed-out worker
cannot mutate a later run. Server tokens remain request headers in memory and
never appear in returned metadata or diagnostics. Provider-specific live notebook
exports (Colab/Kaggle) are delegated to notebook_providers; ordinary unsaved
editor buffers have no provider API.
Main callers: Run initialization before metadata collection and code capture.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib import parse, request

from .runtime import bounded_call

DISCOVERY_TIMEOUT = 3.0
_GUIDANCE = "Set Settings(notebook_name='/path/to/saved.ipynb') or WANDB_NOTEBOOK_NAME to capture the saved notebook."


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _shell():
    module = sys.modules.get("IPython")
    return getattr(module, "get_ipython", lambda: None)() if module else None


def _kernel_and_servers():
    """Read current-kernel and server discovery APIs only when already available."""
    try:
        kernel = importlib.import_module("ipykernel")
        connection = kernel.connect.get_connection_file()
    except (ImportError, AttributeError, ValueError):
        return None, []
    matched = re.fullmatch(r"kernel-(.+)\.json", Path(connection).name)
    if not matched:
        return None, []
    servers = []
    for name in ("jupyter_server.serverapp", "notebook.notebookapp"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        servers.extend(module.list_running_servers())
    return matched.group(1), servers


def _query_sessions(server, timeout):
    """Query the listed server without following redirects with its token."""
    url = parse.urlsplit(server["url"])
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
        raise ValueError("Unsupported Jupyter server origin")
    endpoint = parse.urljoin(server["url"].rstrip("/") + "/", "api/sessions")
    headers = {"Accept": "application/json"}
    if server.get("token"):
        headers["Authorization"] = "token " + str(server["token"])
    opener = request.build_opener(_NoRedirect)
    with opener.open(request.Request(endpoint, headers=headers), timeout=timeout) as response:
        # Session metadata is small; reject an unexpected unbounded response.
        data = response.read(4_000_001)
        if len(data) > 4_000_000:
            raise ValueError("Jupyter session response exceeds its metadata limit")
        return json.loads(data)


def _from_servers(deadline, provider=None):
    kernel_id, servers = _kernel_and_servers()
    if not kernel_id:
        return None
    for server in servers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Notebook discovery deadline reached")
        if server.get("password") and not server.get("token"):
            continue
        try:
            sessions = _query_sessions(server, min(3.0, remaining))
        except (OSError, ValueError, KeyError):
            # Other listed servers may be stale or may host a different kernel.
            # If none succeeds the caller reports one sanitized diagnostic.
            continue
        if not isinstance(sessions, list):
            continue
        for session in sessions:
            if not isinstance(session, dict) or not isinstance(session.get("kernel"), dict) or session["kernel"].get("id") != kernel_id:
                continue
            notebook = session.get("notebook")
            path = notebook.get("path") if isinstance(notebook, dict) else session.get("path") if session.get("type") == "notebook" else None
            if not isinstance(path, str) or not path:
                continue
            if provider == "colab":
                name = notebook.get("name") if isinstance(notebook, dict) else session.get("name")
                return path, name
            root = Path(server.get("root_dir") or server.get("notebook_dir") or os.getcwd()).expanduser().resolve()
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return candidate, root
    return None


def resolve_notebook(settings):
    """Resolve one saved path, preserving explicit precedence and bounded init.

    Main callers: Run.__init__. Failures produce a static, credential-free
    diagnostic for Run's warning policy; normal scripts do no discovery work.
    No source is read here. The existing log_code path owns snapshot/upload.
    """
    settings._notebook_path = None
    settings._notebook_root = None
    settings._notebook_discovery_error = None
    settings._notebook_provider = None
    settings._notebook_jupyter_path = None
    settings._notebook_export_name = None
    if settings.disable_code or settings.mode == "disabled":
        return
    shell = _shell()
    if shell is not None and hasattr(shell, "kernel"):
        from .notebook_providers import provider_name
        try:
            settings._notebook_provider = provider_name()
        except Exception:
            settings._notebook_discovery_error = "Notebook provider import failed; hosted notebook export is unavailable."
    if settings.notebook_name:
        candidate = Path(settings.notebook_name).expanduser().resolve()
        if candidate.is_file():
            settings._notebook_path = str(candidate)
            settings._notebook_root = str(candidate.parent)
        else:
            settings._notebook_discovery_error = "The explicit notebook_name does not identify an existing saved file. " + _GUIDANCE
        return
    shell = _shell()
    if shell is None or not hasattr(shell, "kernel"):
        return
    if settings._notebook_provider:
        provider = settings._notebook_provider
        settings._notebook_root = '/content' if provider == 'colab' else '/kaggle/working'
        settings._notebook_export_name = provider + '.ipynb'
        if provider == 'colab':
            budget = min(DISCOVERY_TIMEOUT, settings.init_timeout)
            deadline = time.monotonic() + budget
            try:
                location = bounded_call(lambda: _from_servers(deadline, provider='colab'), budget, 'notebook discovery')
                if location:
                    from .notebook_providers import export_name
                    settings._notebook_jupyter_path = location[0]
                    settings._notebook_export_name = export_name(location[1], 'colab.ipynb')
            except Exception:
                settings._notebook_discovery_error = 'Colab notebook name discovery failed; exports will use colab.ipynb.'
        return
    namespace = getattr(getattr(shell.kernel, "shell", None), "user_ns", {})
    path = namespace.get("__vsc_ipynb_file__")
    if path:
        candidate = Path(path).expanduser().resolve()
        if candidate.is_file():
            settings._notebook_path = str(candidate)
            settings._notebook_root = str(candidate.parent)
        else:
            settings._notebook_discovery_error = "VSCode reported a notebook that has not been saved locally. " + _GUIDANCE
        return
    budget = min(DISCOVERY_TIMEOUT, settings.init_timeout)
    deadline = time.monotonic() + budget
    try:
        result = bounded_call(lambda: _from_servers(deadline), budget, "notebook discovery")
    except Exception:
        settings._notebook_discovery_error = "Notebook discovery failed or exceeded its time limit. " + _GUIDANCE
        return
    if result is None:
        settings._notebook_discovery_error = "No saved notebook matched the current Jupyter kernel. " + _GUIDANCE
        return
    path, root = result
    settings._notebook_path, settings._notebook_root = str(path), str(root)
