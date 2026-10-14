"""Publish completed Gym recordings through the ordinary video journal.

Architecture: monitor selects the installed recorder -> patches completion hooks
once -> captures the run owning each recording -> Run.log snapshots the video.
Modern Gymnasium saves each segment in stop_recording; legacy Gym/Gymnasium use
VideoRecorder.close. Neither adapter creates environments or changes schedules.
Main callers: init(monitor_gym=True), wandb.gym.monitor and integration.gym.monitor.
Why this exists: the pinned W&B close hook uses fields removed in Gymnasium 1.0
and an ImageEncoder.enabled field absent in Gym 0.25. Hook the actual completion
boundary instead, preserving recording, encoding errors and cleanup.
"""
from __future__ import annotations

from functools import wraps
import importlib
from pathlib import Path
import re
import threading
import warnings
import weakref

from ...errors import Error

_patches = {}
_lock = threading.RLock()


def _library():
    """Prefer Gymnasium, but distinguish absence from a broken installation."""
    for name in ("gymnasium", "gym"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
    raise Error("Couldn't import gymnasium or gym; install the optional gym dependency to monitor recordings")


def _owner():
    import backprop
    active = backprop.run
    return weakref.ref(active) if active is not None else None


def _pending(recorder):
    # Gym wrappers forward unknown attributes to their environment. Keep all
    # adapter state on the recorder itself rather than inheriting another state.
    return recorder.__dict__.setdefault("_backprop_gym_pending", [])


def _publish_pending(recorder):
    """Snapshot each finalized file once, leaving failed local writes retryable.

    Main callers: patched completion, start and close. Run.log commits locally;
    network retries and restarts then reuse the existing durable journal/event ID.
    A recorder owned by a finished run must never attach to a newer experiment.
    """
    import backprop
    pending = _pending(recorder)
    while pending:
        path, owner = pending[0]
        active = owner() if owner is not None else backprop.run
        if owner is not None and (active is None or active._closed):
            warnings.warn("Gym recording completed after its Backprop run finished; close the recorder before finishing the run", RuntimeWarning, stacklevel=3)
        elif active is not None:
            match = re.match(r".+(video\.\d+).+", str(path))
            key = match.group(1) if match else "videos"
            active.log({key: backprop.Video(str(path))})
        pending.pop(0)


def _replace(recorder, name, wrapper, record):
    original = getattr(recorder, name)
    replacement = wraps(original)(wrapper(original))
    record["methods"].append((name, name in recorder.__dict__, original, replacement))
    setattr(recorder, name, replacement)


def _legacy(recorder, record):
    """Hook the recorder, not its encoder, so disabled/empty/broken states work."""
    def constructor(original):
        def wrapped(self, *args, **kwargs):
            self.__dict__["_backprop_gym_owner"] = _owner()
            return original(self, *args, **kwargs)
        return wrapped

    def close(original):
        def wrapped(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            if not record["active"] or self.__dict__.get("_backprop_gym_finalizing"):
                return result
            if (self.enabled and not getattr(self, "broken", False)
                    and not getattr(self, "empty", False)
                    and (not hasattr(self, "recorded_frames") or bool(self.recorded_frames))
                    and not getattr(self, "ansi_mode", False)
                    and not self.__dict__.get("_backprop_gym_queued")):
                _pending(self).append((Path(self.path), self.__dict__.get("_backprop_gym_owner")))
                self.__dict__["_backprop_gym_queued"] = True
            _publish_pending(self)
            return result
        return wrapped

    def destructor(original):
        def wrapped(self):
            self.__dict__["_backprop_gym_finalizing"] = True
            return original(self)
        return wrapped

    _replace(recorder, "__init__", constructor, record)
    _replace(recorder, "close", close, record)
    if hasattr(recorder, "__del__"):
        _replace(recorder, "__del__", destructor, record)


def _modern(recorder, record):
    """Capture each segment before Gymnasium clears its filename and frames."""
    def start(original):
        def wrapped(self, *args, **kwargs):
            if record["active"]:
                _publish_pending(self)
            result = original(self, *args, **kwargs)
            self.__dict__["_backprop_gym_owner"] = _owner()
            return result
        return wrapped

    def stop(original):
        def wrapped(self, *args, **kwargs):
            capture = record["active"] and self.recording and bool(self.recorded_frames)
            if capture:
                path = Path(self.video_folder) / (self._video_name + ".mp4")
                owner = self.__dict__.get("_backprop_gym_owner")
            result = original(self, *args, **kwargs)
            if capture:
                _pending(self).append((path, owner))
                _publish_pending(self)
            return result
        return wrapped

    def close(original):
        def wrapped(self, *args, **kwargs):
            if record["active"]:
                _publish_pending(self)
            return original(self, *args, **kwargs)
        return wrapped

    _replace(recorder, "start_recording", start, record)
    _replace(recorder, "stop_recording", stop, record)
    _replace(recorder, "close", close, record)


def monitor():
    """Automatically log videos saved by Gym/Gymnasium recording wrappers.

    Main callers: init(monitor_gym=True) or an explicit monitor() before training.
    Keep using the framework's RecordVideo wrapper to select episodes and frames.
    Close it before finishing the run. Repeated monitor calls are idempotent;
    teardown restores methods, while ordinary finish allows subsequent sessions.
    """
    library = _library()
    wrappers = importlib.import_module(library.__name__ + ".wrappers")
    # Gymnasium 1.0 moved recording into this wrapper. Detect that public
    # lifecycle rather than adding a version-parser dependency to basic logging.
    modern = library.__name__ == "gymnasium" and hasattr(wrappers.RecordVideo, "stop_recording")
    recorder = wrappers.RecordVideo if modern else importlib.import_module(
        library.__name__ + ".wrappers.monitoring.video_recorder").VideoRecorder
    with _lock:
        if recorder in _patches:
            return
        record = {"active": True, "methods": []}
        try:
            (_modern if modern else _legacy)(recorder, record)
        except Exception:
            _restore(recorder, record)
            raise
        _patches[recorder] = record


def _restore(recorder, record):
    record["active"] = False
    for name, owned, original, replacement in reversed(record["methods"]):
        if getattr(recorder, name) is replacement:
            if owned:
                setattr(recorder, name, original)
            else:
                delattr(recorder, name)


def _unpatch():
    """Restore only our hooks; leave any later third-party replacement intact.

    Main caller: process teardown. Kept private because W&B exposes monitor only.
    """
    with _lock:
        for recorder, record in _patches.items():
            _restore(recorder, record)
        _patches.clear()
