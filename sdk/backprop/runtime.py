"""Bounded lifecycle calls and process-local console capture.

Architecture: one stream wrapper fans output to active runs -> each run journals
console events and maintains an uploadable output file. Named logging handlers
are independent of stdout capture. Shutdown restores streams/handlers before
the last upload, so finished runs never collect later output.
Main callers: Run startup, flush, write_logs and finish.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sys
import threading
import time
from .terminal import Terminal
from .console_styles import SGRFileRenderer


def bounded_call(function, timeout, description):
    """Wait at most timeout seconds while preserving errors from the operation.

    Main callers: init's read/upload and finish's final upload. Python cannot
    terminate an in-flight network call; it completes in a daemon thread. The
    durable journal keeps ownership of pending work and prevents duplicate IDs.
    """
    if not timeout:
        return function()
    completed = threading.Event()
    outcome = []

    def execute():
        try:
            outcome.append((True, function()))
        except BaseException as error:
            outcome.append((False, error))
        finally:
            completed.set()

    threading.Thread(target=execute, name="backprop-" + description, daemon=True).start()
    if not completed.wait(max(0, timeout)):
        raise TimeoutError(f"Backprop {description} exceeded {timeout:g} seconds; unsent events remain in the local journal")
    successful, value = outcome[0]
    if not successful:
        raise value
    return value


class _WrappedStream:
    def __init__(self, original, stream):
        self.original, self.stream = original, stream

    def write(self, text):
        if getattr(_writing, "diagnostic", False):
            return _write_uncaptured(text, self.stream, self.original)
        result = self.original.write(text)
        _broadcast(text, self.stream, redirected=False)
        return result

    def flush(self):
        return self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


_captures = []
_capture_lock = threading.RLock()
_writing = threading.local()
_wrappers = {}
_redirectors = {}


@contextmanager
def _suppress_capture():
    """Keep this thread's SDK diagnostics outside every active run's log.

    Main callers: report_background_warning. Suppression is thread-local so
    other training threads keep logging. Wrapped writes bypass a redirector's
    pipe as well, since a later pipe-reader thread cannot inherit this flag.
    """
    previous = getattr(_writing, "diagnostic", False)
    _writing.diagnostic = True
    try:
        yield
    finally:
        _writing.diagnostic = previous


def _write_uncaptured(text, stream, original=None):
    """Write a diagnostic without sending it through the native capture pipe.

    Main callers: wrapped streams under suppression and warning failure reports.
    The redirector protects its saved descriptor against concurrent shutdown.
    Without native redirection, preserve the application's chosen Python stream.
    """
    redirector = _redirectors.get(stream)
    if redirector is not None and redirector.write_uncaptured(text):
        return len(text)
    target = original if original is not None else getattr(sys, stream)
    while isinstance(target, _WrappedStream):
        target = target.original
    return target.write(text)


def report_background_warning(run, message):
    """Report upload failures without letting diagnostics alter retry policy.

    Main callers: Run._background. Normal warning filters and hooks still run.
    If warnings are configured as errors or a hook fails, retain that failure
    in the run's warnings and attempt an uncaptured stderr diagnostic. A broken
    stderr is also retained there, so even reporting failures cannot prevent the
    configured fatal-stop callback or terminate a recoverable retry worker.
    """
    with _suppress_capture():
        try:
            run._warn(message)
        except Exception as error:
            diagnostic = f"Backprop warning reporting failed ({type(error).__name__}: {error}); {message}"
            run._warnings.append(diagnostic)
            try:
                _write_uncaptured(diagnostic + "\n", "stderr")
            except Exception as output_error:
                run._warnings.append(f"Backprop could not write its diagnostic to stderr: {type(output_error).__name__}: {output_error}")


def _broadcast(text, stream, *, redirected):
    if not text or getattr(_writing, "active", False) or getattr(_writing, "diagnostic", False):
        return
    _writing.active = True
    try:
        for capture in list(_captures):
            if (capture.mode == "redirect") == redirected and capture.mode != "off":
                capture.run.log_console(text, stream)
    finally:
        _writing.active = False


class _Redirector:
    """Capture native/subprocess writes while forwarding bytes to the old fd."""

    def __init__(self, stream, descriptor):
        self.stream, self.descriptor = stream, descriptor
        self.saved = os.dup(descriptor)
        self.output_lock = threading.Lock()
        self.output_closed = False
        self.read, write = os.pipe()
        os.dup2(write, descriptor)
        os.close(write)
        self.thread = threading.Thread(target=self._read, name="backprop-console-" + stream, daemon=True)
        self.thread.start()

    def _read(self):
        import codecs
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while data := os.read(self.read, 8192):
                self._forward(data)
                _broadcast(decoder.decode(data), self.stream, redirected=True)
            final = decoder.decode(b"", final=True)
            if final:
                _broadcast(final, self.stream, redirected=True)
        finally:
            os.close(self.read)
            with self.output_lock:
                self.output_closed = True
                os.close(self.saved)

    def _forward(self, data):
        with self.output_lock:
            if self.output_closed:
                return False
            view = memoryview(data)
            while view:
                view = view[os.write(self.saved, view):]
            return True

    def write_uncaptured(self, text):
        return self._forward(text.encode("utf-8", "backslashreplace"))

    def stop(self):
        getattr(sys, self.stream).flush()
        with self.output_lock:
            os.dup2(self.saved, self.descriptor)
        self.thread.join(timeout=1)
        # A child may retain the pipe writer. The reader keeps forwarding and
        # owns both descriptors until that child closes its inherited output.


class _LoggerHandler(logging.Handler):
    def __init__(self, run, level):
        super().__init__(level)
        self.run = run
        self.setFormatter(logging.Formatter(logging.BASIC_FORMAT))

    def emit(self, record):
        if not getattr(_writing, "diagnostic", False):
            self.run.write_logs(self.format(record))


class ConsoleCapture:
    """Own capture hooks and output-file rollover for exactly one Run."""

    def __init__(self, run):
        self.run = run
        self.mode = run._settings.console
        if self.mode == "auto":
            self.mode = "wrap"
        self.handlers = []
        self.path = None
        self.size = 0
        self.part = 0
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.terminals = {stream: Terminal() for stream in ("stdout", "stderr")}
        self.records = []
        self.file_renderer = SGRFileRenderer()
        self.rotation_timer = None
        self.pending_parts = []

    def start(self):
        if self.run.disabled:
            return
        with _capture_lock:
            _captures.append(self)
            if self.mode == "redirect":
                for stream, descriptor in (("stdout", 1), ("stderr", 2)):
                    if stream not in _redirectors:
                        getattr(sys, stream).flush()
                        _redirectors[stream] = _Redirector(stream, descriptor)
            # Redirect also needs wrappers: diagnostic writes must bypass the
            # pipe, while ordinary Python/native bytes still enter it once.
            if self.mode != "off":
                for stream in ("stdout", "stderr"):
                    if stream not in _wrappers:
                        wrapper = _WrappedStream(getattr(sys, stream), stream)
                        _wrappers[stream] = wrapper
                        setattr(sys, stream, wrapper)
        for name, level in (self.run._settings.capture_loggers or {}).items():
            handler = _LoggerHandler(self.run, level)
            logger = logging.getLogger(name)
            logger.addHandler(handler)
            self.handlers.append((logger, handler))

    def write(self, text, stream):
        if getattr(_writing, "diagnostic", False):
            return
        # Serialize rendering and journal order together. The global order is
        # always Run -> Console; poll releases Console before entering Run.
        with self.run._lock:
            if self.run._closed:
                return
            self._write_locked(text, stream)

    def _write_locked(self, text, stream):
        """Append console output and rotate immutable multipart files when due."""
        # Never acquire the Run lock while holding the console lock: warnings
        # emitted inside Run.log can enter console capture in the opposite order.
        remove = 0
        if self.mode == "wrap_emu":
            with self.lock:
                remove, text = self.terminals[stream].feed(text)
            if not remove and not text:
                return
        self.run._emit("console", {"text": text, "stream": stream, **({"replace_tail": remove} if remove else {})})
        register = None
        with self.lock:
            if self.path is None:
                self._open_part()
                if not self.run._settings.console_multipart:
                    register = self.path
            self._write_file(text, stream, remove)
        if register is not None:
            self.run._files.save(register, base_path=self.run.dir, policy="live", glob=False)
        self.poll()

    def _write_file(self, text, stream, remove):
        """Patch the affected file tail, restoring its per-stream SGR checkpoint.

        Main callers: _write_locked. Event edits operate on serialized Unicode
        text, but a file cannot interleave half an escape with another stream.
        Emulated records therefore store a parser checkpoint with their byte
        offset; appends parse only the new record and edits parse only the tail.
        """
        first = len(self.records)
        remaining = remove
        for index in range(len(self.records)-1, -1, -1):
            if not remaining:
                break
            record = self.records[index]
            if record[0] != stream:
                continue
            count = min(remaining, len(record[1]))
            record[1] = record[1][:-count] if count else record[1]
            remaining -= count
            first = index
        if remaining:
            raise RuntimeError("Terminal edit exceeds the current console part")
        offset = self.records[first][2] if first < len(self.records) else self.size
        renderer = (self.records[first][3] if first < len(self.records) else self.file_renderer).copy() if self.mode == "wrap_emu" else None
        self.records.append([stream, text, self.size, None])
        with self.path.open("r+b") as target:
            target.seek(offset)
            for record in self.records[first:]:
                record[2] = target.tell()
                if renderer is not None:
                    record[3] = renderer.copy()
                    content = renderer.feed(record[1], record[0])
                else:
                    content = record[1]
                target.write(content.encode("utf-8"))
            target.truncate()
            self.size = target.tell()
        if renderer is not None:
            self.file_renderer = renderer

    def _open_part(self):
        settings = self.run._settings
        self.started = time.monotonic()
        self.size = 0
        self.records = []
        self.file_renderer = SGRFileRenderer()
        if settings.console_multipart:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            name = f"logs/output_{stamp}_{self.part:04d}.log"
            self.part += 1
        else:
            name = "output.log"
        self.path = Path(self.run.dir) / name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        if settings.console_multipart and settings.console_chunk_max_seconds:
            self.rotation_timer = threading.Timer(settings.console_chunk_max_seconds, self._idle_rollover)
            self.rotation_timer.daemon = True
            self.rotation_timer.start()

    def _idle_rollover(self):
        """Close an elapsed console part even while training produces no output."""
        try:
            with self.run._lock:
                if not self.run._closed:
                    self.poll()
        except Exception as error:
            self.run._warn(f"Console part could not be persisted; flush or finish retries it: {error}")

    def poll(self, ending=False):
        with self.lock:
            settings = self.run._settings
            if self.path is not None and settings.console_multipart:
                size_due = settings.console_chunk_max_bytes and self.size >= settings.console_chunk_max_bytes
                time_due = settings.console_chunk_max_seconds and time.monotonic() - self.started >= settings.console_chunk_max_seconds
                if ending or size_due or time_due:
                    self.pending_parts.append(self.path)
                    self.path = None
                    if self.rotation_timer is not None:
                        self.rotation_timer.cancel()
                        self.rotation_timer = None
                    self.terminals = {stream: Terminal() for stream in ("stdout", "stderr")}
            pending = list(self.pending_parts)
        for path in pending:
            with self.run._lock:
                with self.lock:
                    if path not in self.pending_parts:
                        continue
                self.run._files.save(path, base_path=self.run.dir, policy="now", glob=False)
                with self.lock:
                    self.pending_parts.remove(path)

    def stop(self):
        for logger, handler in self.handlers:
            logger.removeHandler(handler)
            handler.close()
        self.handlers.clear()
        with _capture_lock:
            # Drain redirected bytes before removing their last subscriber.
            if self.mode == "redirect" and not any(c is not self and c.mode == "redirect" for c in _captures):
                for redirector in _redirectors.values():
                    redirector.stop()
                _redirectors.clear()
            if self in _captures:
                _captures.remove(self)
            if not any(c.mode != "off" for c in _captures):
                for stream, wrapper in _wrappers.items():
                    if getattr(sys, stream) is wrapper:
                        setattr(sys, stream, wrapper.original)
                _wrappers.clear()
        self.poll(ending=True)
