"""Short-lived snapshots of global and workspace INI settings.

Architecture: read both files -> overlay local keys -> explicit set/clear calls
mark one or both snapshots dirty -> save writes only changed files atomically.
Reading never writes. These are configuration files, not an authentication cache.
Main callers: Settings.read_system_settings/update_from_system_settings.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
import tempfile
import warnings

from .errors import Error


class SaveSettingsError(Error):
    pass


class SettingsFiles:
    def __init__(self, *, global_settings, local_settings):
        self._paths = {"global": Path(global_settings) if global_settings else None, "local": Path(local_settings)}
        self._values, self._dirty = {}, set()
        self.sources = []
        for kind, path in self._paths.items():
            values = self._read(path)
            self._values[kind] = values
            if values:
                self.sources.append(path)

    @staticmethod
    def _read(path):
        if path is None:
            return {}
        parser = configparser.ConfigParser()
        try:
            if not path.exists():
                return {}
            with path.open() as stream:
                parser.read_file(stream)
            return dict(parser["default"]) if parser.has_section("default") else {}
        except (OSError, configparser.Error) as error:
            warnings.warn(f"Error reading settings at {path}: {error}", RuntimeWarning)
            return {}

    def all(self):
        return {**self._values["global"], **self._values["local"]}

    def set(self, key, value, *, globally=False):
        kind = "global" if globally and self._paths["global"] else "local"
        if kind == "global":
            self.clear(key)
        if self._values[kind].get(key) != value:
            self._values[kind][key] = value
            self._dirty.add(kind)

    def clear(self, key, *, globally=False):
        for kind in (("local", "global") if globally else ("local",)):
            if key in self._values[kind]:
                del self._values[kind][key]
                self._dirty.add(kind)

    def save(self):
        for kind in tuple(self._dirty):
            path = self._paths[kind]
            if path is None:
                continue
            temporary = None
            try:
                parser = configparser.ConfigParser()
                parser["default"] = self._values[kind]
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as target:
                    temporary = Path(target.name)
                    parser.write(target)
                    target.flush()
                    os.fsync(target.fileno())
                temporary.replace(path)
                self._dirty.remove(kind)
            except (OSError, configparser.Error) as error:
                if temporary:
                    temporary.unlink(missing_ok=True)
                raise SaveSettingsError(f"Error updating settings at {path}: {error}") from error
