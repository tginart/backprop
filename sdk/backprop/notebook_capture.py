"""Notebook cell callbacks, display capture and final immutable snapshots.

Architecture: one publisher wrapper fans display events to active runs; each run
owns its post_run_cell callback. Refresh/finish obtains a read-only bounded
snapshot, then the caller stages bytes through normal run-file/artifact APIs.
Finishing in any order unregisters only that run. The last subscriber restores
the original publisher; failure paths detach callbacks without a late upload.
Main callers: Run startup/finish and init(save_code=True).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time

from .apis.public.files import write_atomic
from .notebook_discovery import _shell
from .notebook_snapshot import NotebookRedactor, CaptureLimitError, MAX_NOTEBOOK_BYTES, saved_notebook, execution_history
from .runtime import bounded_call

CAPTURE_TIMEOUT = 3.0
_hubs = {}
_hub_lock = threading.RLock()


def _failure_message(error):
    if isinstance(error, TimeoutError):
        reason = "exceeded its deadline"
    elif isinstance(error, CaptureLimitError):
        reason = "exceeded its size/cell limit"
    else:
        reason = "failed"
    return f"Notebook snapshot {reason}; previous snapshots remain available ({type(error).__name__})"


class _DisplayHub:
    def __init__(self, publisher):
        self.publisher = publisher
        self.original = publisher.publish
        self.listeners = []
        def publish(data, metadata=None, *args, **kwargs):
            result = self.original(data, metadata, *args, **kwargs)
            with _hub_lock:
                listeners = list(self.listeners)
            for listener in listeners:
                listener.display(data, metadata)
            return result
        self.wrapper = publish
        publisher.publish = publish

    def remove(self, listener):
        with _hub_lock:
            if listener in self.listeners:
                self.listeners.remove(listener)
            if not self.listeners:
                if self.publisher.publish is self.wrapper:
                    self.publisher.publish = self.original
                _hubs.pop(id(self.publisher), None)


class NotebookCapture:
    def __init__(self, run):
        self.run = run
        self.shell = _shell()
        self.enabled = bool(run._settings.save_code and not run._settings.disable_code and not run.disabled)
        self.redactor = NotebookRedactor(run)
        self.outputs = {}
        self._output_bytes = 0
        self._display_error = False
        self._hub = None
        self._registered = False
        self._closed = False
        self._lock = threading.RLock()
        self._callback = self._post_cell

    def start(self):
        if not self.enabled or self.shell is None or not hasattr(self.shell, "kernel"):
            return
        events, publisher = getattr(self.shell, "events", None), getattr(self.shell, "display_pub", None)
        if events is None or publisher is None:
            return
        try:
            events.register("post_run_cell", self._callback)
            self._registered = True
            with _hub_lock:
                hub = _hubs.get(id(publisher))
                if hub is None:
                    hub = _hubs[id(publisher)] = _DisplayHub(publisher)
                hub.listeners.append(self)
                self._hub = hub
        except Exception:
            self.close()
            self.run._warn("Notebook callback setup failed; automatic cell refresh is unavailable")

    def display(self, data, metadata):
        with self._lock:
            if self._closed or self._display_error:
                return
            try:
                output = self.redactor.value({"data": data, "metadata": metadata or {}})
                size = len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode())
                if self._output_bytes + size > MAX_NOTEBOOK_BYTES:
                    raise ValueError("Notebook display capture exceeds the size limit")
                self.outputs.setdefault(self.shell.execution_count, []).append(output)
                self._output_bytes += size
            except Exception:
                self._display_error = True
                self.run._warn("Notebook display capture failed or exceeded its size limit; session history will not be saved incompletely")

    def _post_cell(self, *args, **kwargs):
        if not self._closed and not self.run._closed:
            self.refresh()

    def _snapshot(self, include_history, deadline, provider_snapshot=None):
        self.redactor.refresh(self.run)
        with self._lock:
            incomplete_displays = self._display_error
            outputs = copy.deepcopy(self.outputs) if include_history else {}
        result, problems = dict([provider_snapshot]) if provider_snapshot else {}, []
        path = getattr(self.run._settings, "_notebook_path", None)
        if path:
            try:
                result[Path(path).name] = saved_notebook(path, self.redactor)
            except Exception as error:
                problems.append(_failure_message(error))
        if include_history and self.shell is not None:
            if incomplete_displays:
                problems.append("Notebook history was not saved because its display capture is incomplete")
            else:
                try:
                    history = execution_history(self.shell, outputs, self.redactor, deadline)
                    if history is not None:
                        result["_session_history.ipynb"] = history
                except Exception as error:
                    problems.append(_failure_message(error))
        return result, problems

    def refresh(self, *, include_history=False, deadline=None):
        """Stage only snapshots returned before the deadline, then log code.

        Main callers: init, post_run_cell and finish. Background workers read and
        encode only; this caller exclusively owns persistent writes and events.
        """
        if not self.enabled or self._closed or self.run._closed:
            return False
        provider = getattr(self.run._settings, "_notebook_provider", None)
        from .notebook_providers import COLAB_TIMEOUT, export_notebook
        limit = max(CAPTURE_TIMEOUT, COLAB_TIMEOUT) if provider == "colab" else CAPTURE_TIMEOUT
        budget = limit if deadline is None else min(limit, deadline - time.monotonic())
        if budget <= 0:
            self.run._warn("Notebook capture deadline expired; previous snapshots remain available")
            return False
        stop_at = time.monotonic() + budget
        try:
            provider_snapshot = None
            provider_problems = []
            path = getattr(self.run._settings, "_notebook_path", None)
            if provider and not (path and Path(path).is_file()):
                self.redactor.refresh(self.run)
                try:
                    provider_snapshot = export_notebook(self.run._settings, self.redactor, stop_at)
                except Exception as error:
                    provider_problems.append(_failure_message(error))
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Notebook capture deadline expired")
            snapshots, problems = bounded_call(lambda: self._snapshot(include_history, stop_at, provider_snapshot), remaining, "notebook capture")
            for problem in [*provider_problems, *problems]:
                self.run._warn(problem)
            if not snapshots:
                return False
            stage = Path(self.run.sync_dir) / "tmp/code"
            stage.mkdir(parents=True, exist_ok=True)
            for name, data in snapshots.items():
                write_atomic(stage / name, data)
            if "_session_history.ipynb" in snapshots:
                history = Path(self.run.dir) / "code/_session_history.ipynb"
                write_atomic(history, snapshots["_session_history.ipynb"])
                saved = self.run.save(str(history), base_path=self.run.dir, policy="now", glob=False)
                if saved:
                    self.run.config.update({"_wandb": {**self.run.config.get("_wandb", {}),
                        "session_history": "code/_session_history.ipynb"}}, allow_val_change=True)
            self.run.log_code(root=None)
            return True
        except Exception as error:
            self.run._warn(_failure_message(error))
            return False

    def _detach(self):
        try:
            if self._registered:
                self.shell.events.unregister("post_run_cell", self._callback)
        except ValueError:
            pass  # An external owner already removed this exact callback.
        finally:
            self._registered = False
            if self._hub is not None:
                self._hub.remove(self)
                self._hub = None

    def finish(self, deadline=None):
        """Detach first, capture final history, and release state even on error."""
        if self._closed:
            return
        try:
            self._detach()
            self.refresh(include_history=True, deadline=deadline)
        finally:
            self.close()

    def close(self):
        self._detach()
        with self._lock:
            self._closed = True
            self.outputs.clear()
