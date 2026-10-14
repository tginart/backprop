"""Run-local diagnostic files and latest-run links.

Architecture: Run lifecycle calls -> isolated user/internal loggers -> append-only
debug files. Atomic relative links point at the newest initialized run. Closing
one run removes only its own handlers; other runs keep their logs and no process
root logger is modified. Diagnostics redact known authentication values and never
dump the run configuration or event payloads.
Main callers: Run initialization, upload diagnostics, warnings and finalization.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import threading
import uuid
import warnings


def _replace_link(link, target):
    """Atomically retarget a relative link, leaving its previous target intact.

    Main callers: DebugLogs.start. Unsupported symlinks produce a visible warning
    while the actual diagnostic files remain usable, including on Windows.
    """
    link, target = Path(link), Path(target)
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name("." + link.name + "-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(os.path.relpath(target, link.parent), target_is_directory=target.is_dir())
        temporary.replace(link)
    except (OSError, NotImplementedError) as error:
        warnings.warn(f"Could not create diagnostic link {link}: {type(error).__name__}; log files remain available", RuntimeWarning, stacklevel=3)
    finally:
        temporary.unlink(missing_ok=True)


class DebugLogs:
    """Own two file handlers for exactly one Run and close them idempotently."""

    def __init__(self, run):
        self.settings = run.settings
        self.run_id = run.id
        self._lock = threading.RLock()
        self._loggers = {}
        self._closed = False
        values = [self.settings.api_key, getattr(run._sink, "token", None)]
        values.extend((self.settings.azure_account_url_to_access_key or {}).values())
        values.extend(os.environ.get(name) for name in ("FOUNDRY_TOKEN", "BACKPROP_TOKEN", "WANDB_API_KEY"))
        self._secrets = sorted({value for value in values if isinstance(value, str) and value}, key=len, reverse=True)

    def start(self):
        """Create real files before installing optional latest-run links.

        Main callers: Run.__init__, once its journal exists. If a file cannot be
        opened, close every handler already opened and propagate the failure.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("Cannot reopen closed run debug logs")
            if self._loggers:
                return
            try:
                for kind, path in (("user", self.settings.log_user), ("internal", self.settings.log_internal)):
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    logger = logging.Logger(f"backprop.{self.run_id}.{kind}", level=logging.DEBUG)
                    logger.propagate = False
                    handler = logging.FileHandler(path, encoding="utf-8")
                    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
                    logger.addHandler(handler)
                    self._loggers[kind] = logger
                self.user(f"Run {self.run_id} diagnostics initialized")
                self.internal("Native event journal diagnostics initialized")
                if self.settings.symlink:
                    for link, target in ((self.settings.sync_symlink_latest, self.settings.sync_dir),
                                         (self.settings.log_symlink_user, self.settings.log_user),
                                         (self.settings.log_symlink_internal, self.settings.log_internal)):
                        _replace_link(link, target)
            except BaseException:
                self.close()
                raise

    def _write(self, kind, message, level):
        with self._lock:
            # A timed-out finish can leave an upload worker completing later.
            # It must not reopen or append to a run already closed by its owner.
            if self._closed:
                return
            text = str(message)
            for secret in self._secrets:
                text = text.replace(secret, "[redacted]")
            text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[redacted]", text)
            self._loggers[kind].log(level, text)

    def user(self, message, level=logging.INFO):
        self._write("user", message, level)

    def internal(self, message, level=logging.DEBUG):
        self._write("internal", message, level)

    def close(self):
        """Flush and detach only this run's handlers, including partial setup."""
        with self._lock:
            if self._closed:
                return
            for logger in self._loggers.values():
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()
            self._closed = True
