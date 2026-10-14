"""Portable W&B code markers without a telemetry service.

Architecture: one initialization probe -> exact pinned marker parser -> validated
code/repository/version strings -> native run metadata and public rawconfig view.
Scripts use only __main__.__doc__; notebooks use only their first saved/exported
cell. The probe is independent of save_code. label_disable and disabled runs skip
it completely. Each process starts a fresh label state, including resumed runs.
Main callers: Run initialization, run_metadata.save_metadata and public Run.rawconfig.

Why this exists: W&B stores these public-setting effects in private telemetry.
The same values can use our existing metadata events; no protobuf dependency or
additional server is necessary. The parser follows W&B v0.30.0 telemetry.py:57.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
import time

from .runtime import bounded_call

_LABEL_TOKEN = "@wandbcode{"
_MATCH_RE = re.compile(r"(?P<code>[a-zA-Z0-9_-]+)[,}](?P<rest>.*)")
_VALID_LABEL = re.compile(r"[a-zA-Z0-9_-]+$")
_LABEL_FIELDS = {"code": "1", "repo": "2", "code_version": "3"}


def parse_label_lines(lines):
    """Parse the pinned permissive marker syntax, including its stopping rule.

    Main callers: labels_from_lines. Nonmarker lines before the first marker are
    ignored; the first nonmarker line afterward ends the contiguous group.
    """
    seen, result = False, {}
    for line in lines:
        index = line.find(_LABEL_TOKEN)
        if index < 0:
            if seen:
                break
            continue
        seen = True
        label = line[index + len(_LABEL_TOKEN):]
        matched = _MATCH_RE.match(label)
        if matched:
            result["code"] = matched.group("code").replace("-", "_")
            label = matched.group("rest")
        for key, value in re.findall(r'([a-zA-Z0-9_]+)\s*=\s*("[a-zA-Z0-9_-]*"|[a-zA-Z0-9_-]*)[,}]', label):
            result[key] = value.strip('"').replace("-", "_")
    return result


def labels_from_lines(lines):
    """Select the three supported labels using the pinned truthy alias order."""
    parsed = parse_label_lines(lines)
    selected = {"code": parsed.get("code") or parsed.get("c"),
                "repo": parsed.get("repo") or parsed.get("r"),
                "code_version": parsed.get("version") or parsed.get("v")}
    return {key: value for key, value in selected.items() if value and _VALID_LABEL.match(value)}


def notebook_lines(notebook):
    """Use only cell zero; preserve the pinned string-source split() behavior."""
    source = notebook.get("cells", [])[0].get("source")
    return source.split() if isinstance(source, str) else source


def _notebook_labels(settings, redactor, deadline):
    """Read saved/provider bytes before a deadline, with no persistent writes.

    Main callers: probe_code_labels. Colab export remains on the caller thread;
    ordinary file reads and Kaggle requests retain their existing bounded paths.
    """
    from .notebook_discovery import resolve_notebook
    from .notebook_providers import export_notebook
    from .notebook_snapshot import saved_notebook

    if settings.disable_code:
        # Code saving is separate from this public metadata control. Discover a
        # path on a private settings copy without enabling source-file capture.
        settings = settings.model_copy(deep=True)
        settings.disable_code = False
        settings.init_timeout = max(0.000001, deadline - time.monotonic())
        resolve_notebook(settings)
    path = getattr(settings, "_notebook_path", None)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Code label probe deadline reached")
    if path and Path(path).is_file():
        content = bounded_call(lambda: saved_notebook(path, redactor), remaining, "code label notebook probe")
    elif getattr(settings, "_notebook_provider", None):
        exported = export_notebook(settings, redactor, deadline)
        content = exported[1] if exported else None
    else:
        content = None
    if time.monotonic() > deadline:
        raise TimeoutError("Code label probe deadline reached")
    if content is None:
        return {}
    lines = notebook_lines(json.loads(content))
    return labels_from_lines(lines) if lines else {}


def probe_code_labels(run):
    """Return a fresh process's labels without exposing credentials on failure.

    Main callers: Run.__init__, before its initial metadata is persisted. A
    missing/invalid notebook yields no labels and a local diagnostic, matching
    the source's nonfatal probe. Disabled runs perform no notebook or file work.
    """
    settings = run._settings
    if run.disabled or settings.label_disable:
        return {}
    from .notebook_discovery import _shell
    from .notebook_snapshot import NotebookRedactor
    redactor = NotebookRedactor(run)
    shell = _shell()
    if shell is not None and hasattr(shell, "kernel"):
        try:
            deadline = min(run._init_deadline, time.monotonic() + 5)
            return _notebook_labels(settings, redactor, deadline)
        except Exception as error:
            if run._debug:
                run._debug.internal(f"Code label notebook probe failed ({type(error).__name__})")
            return {}
    docstring = getattr(sys.modules.get("__main__"), "__doc__", None)
    return labels_from_lines(redactor.text(docstring).splitlines()) if isinstance(docstring, str) else {}


def project_rawconfig(config, metadata):
    """Project native labels into the public pinned _wandb.t['9'] location.

    Main callers: public Run.rawconfig. An explicit empty label state removes
    labels from a previous process. Other config/internal telemetry keys remain
    intact, and older events without a native label field remain untouched.
    """
    if "code_labels" not in metadata:
        return config
    result = copy.deepcopy(config)
    internal = result.get("_wandb")
    internal = copy.deepcopy(internal) if isinstance(internal, dict) else {}
    telemetry = internal.get("t")
    telemetry = copy.deepcopy(telemetry) if isinstance(telemetry, dict) else {}
    encoded = {number: metadata["code_labels"][key] for key, number in _LABEL_FIELDS.items()
               if metadata["code_labels"].get(key)}
    telemetry.pop("9", None)
    if encoded:
        telemetry["9"] = encoded
    if telemetry or "t" in internal:
        internal["t"] = telemetry
    if internal or "_wandb" in result:
        result["_wandb"] = internal
    return result
