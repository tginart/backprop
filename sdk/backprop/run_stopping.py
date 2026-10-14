"""Deliver a remote or opted-in fatal stop once per logging process.

Architecture: prime a bounded stream reader -> publish the fresh process ID in
run_start -> poll on an independent daemon -> journal acknowledgement -> callback.
The callback runs outside journal/run locks. Finish cancels future delivery without
waiting for a blocked HTTP request. Custom callbacks need not finish the run; an
accepted acknowledgement therefore never claims that training has exited.
Main callers: Run initialization, fatal upload handling and finish cleanup.
"""
from __future__ import annotations

from datetime import datetime, timezone
import platform
import signal
import threading
import time
import uuid

from .control_store import POLL_INTERVAL, matches
from .runtime import report_background_warning


def interrupt_main():
    """Send SIGINT to the main thread, including when it is in a blocking syscall.

    Main callers: RunStopChecker without a user stop_fn. This mirrors pinned
    W&B sdk/lib/interrupt.py; _thread.interrupt_main alone does not wake sleep.
    """
    if platform.system() == "Windows":
        signal.raise_signal(signal.SIGINT)
    else:
        signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)


class RunStopChecker:
    """One callback gate shared by remote requests and fatal upload failures."""
    def __init__(self, run, *, poll_interval=None):
        self.run = run
        self.instance_id = uuid.uuid4().hex
        self.interval = POLL_INTERVAL if poll_interval is None else poll_interval
        self.reader = None
        self._closed = threading.Event()
        self._gate = threading.Lock()
        self._called = False
        self._thread = None
        self._pending_request = None
        if run.mode in {"online", "shared"} and callable(getattr(run._sink, "control_reader", None)):
            self.reader = run._initial_call(lambda: run._sink.control_reader(
                project=run.project, entity=run.entity, run_id=run.id, instance_id=self.instance_id))

    @property
    def capability(self):
        return {"instance_id": self.instance_id, "stop_supported": self.reader is not None,
                "poll_interval_seconds": POLL_INTERVAL}

    def start(self):
        if self.reader is not None and not self._closed.is_set():
            self._thread = threading.Thread(target=self._loop, name=f"backprop-control-{self.run.id}", daemon=True)
            self._thread.start()

    def stop_soon(self):
        self._closed.set()

    def _loop(self):
        warned = False
        while not self._closed.is_set() and not self.run._stop.is_set():
            started = time.monotonic()
            try:
                if self._pending_request is None:
                    events = self.reader.poll()
                    for event in events:
                        if matches(event, project=self.run.project, entity=self.run.entity,
                                   run_id=self.run.id, instance_id=self.instance_id):
                            self._pending_request = event["event_id"]
                            break
                if self._pending_request is not None:
                    self.trigger(request_id=self._pending_request)
                    return
                warned = False
            except Exception as error:
                if not warned and not self._closed.is_set() and not self.run._stop.is_set():
                    report_background_warning(self.run, f"Backprop stop-request check failed ({type(error).__name__}); checking will retry")
                    warned = True
            self._closed.wait(max(0, self.interval - (time.monotonic()-started)))

    def _ack(self, request_id, status, error=None):
        """Append control status even if an unrelated upload failure is latched.

        Main callers: trigger. This uses the same durable event format as Run
        while avoiding _check's fatal-error guard. Closed runs remain immutable.
        """
        run = self.run
        with run._lock:
            if run._closed or run._stop.is_set():
                return False
            run._journal.append({"schema_version": 1, "event_id": uuid.uuid4().hex,
                "timestamp": datetime.now(timezone.utc).isoformat(), "run_id": run.id,
                "project": run.project, "entity": run.entity, "type": "run_stop_ack",
                "payload": {"instance_id": self.instance_id, "request_id": request_id,
                            "status": status, **({"error": error} if error else {})}})
            run._wake.set()
            return True

    def trigger(self, *, request_id=None):
        """Claim one delivery, persist its request acknowledgement, then call.

        Main callers: checker loop and permanent-upload error handling. A failed
        local acknowledgement leaves the request eligible for a later retry. A
        callback exception is recorded once and never causes another invocation.
        """
        with self._gate:
            if self._called or self._closed.is_set() or self.run._stop.is_set():
                return False
            if request_id is not None and not self._ack(request_id, "accepted"):
                return False
            self._called = True
        try:
            (self.run._settings.stop_fn or interrupt_main)()
        except BaseException as error:
            if request_id is not None:
                try:
                    self._ack(request_id, "failed", type(error).__name__)
                except Exception as ack_error:
                    report_background_warning(self.run, f"Backprop could not persist failed stop status ({type(ack_error).__name__})")
            report_background_warning(self.run, f"Backprop stop callback failed ({type(error).__name__}); callback will not run again")
        return True
