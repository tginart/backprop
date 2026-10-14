"""Config parsing, change guards and detached read-only snapshots.

Architecture: supported input -> normalized JSON-friendly values -> atomic owner
callback -> local dictionary. Locked keys are updated only by their lock owner.
Main callers: init(config=...), run.config updates, public API hydration.
"""
from __future__ import annotations

import copy
import dataclasses
import enum
import json
from pathlib import Path
import warnings

from .errors import ConfigError, UsageError
from .settings import in_notebook


def parse_config(params, exclude=None, include=None):
    """Normalize optional native config before applying the shared public parser.

    Main callers: Run/init, Config.update and config-default files. Native init
    treats absent config and empty defaults as {}, and accepts pathlib paths.
    The public helper retains the pinned strict input and error contract.
    """
    from . import helper
    if exclude and include:
        raise UsageError("Expected at most only one of exclude or include")
    if isinstance(params, (str, Path)):
        params = helper._read_config_file(params)
    return helper.parse_config({} if params is None else params, exclude, include)


def friendly(value):
    """Normalize config values without converting logged media into config data."""
    from .data_types import Artifact, Histogram, Media, Plotly, Table
    from .config_artifacts import is_config_artifact
    if isinstance(value, Artifact):
        return value
    if isinstance(value, (Histogram, Media, Plotly, Table)) or hasattr(value, "_backprop_plot"):
        raise TypeError("WBValue objects cannot be added to the run config")
    if isinstance(value, enum.Enum):
        return friendly(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return friendly(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        if any(is_config_artifact(item) for item in value.values()):
            raise ValueError("Artifacts can only be top-level config values")
        return {str(key): friendly(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if any(is_config_artifact(item) for item in value):
            raise ValueError("Artifacts can only be top-level config values")
        return [friendly(item) for item in value]
    if isinstance(value, slice):
        return {"slice_start": value.start, "slice_step": value.step, "slice_stop": value.stop}
    if value is None or isinstance(value, (bool, str, int, float)):
        return value
    if hasattr(value, "tolist"):
        return friendly(value.tolist())
    if hasattr(value, "item"):
        return friendly(value.item())
    return str(value)


class Config:
    """W&B dictionary/attribute configuration with atomic persisted updates."""

    def __init__(self):
        object.__setattr__(self, "_data", {})
        object.__setattr__(self, "_locked", {})
        object.__setattr__(self, "_owner", None)
        object.__setattr__(self, "_settings", None)
        if Path("config-defaults.yaml").is_file():
            self.update("config-defaults.yaml")

    def _bind(self, owner, settings):
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_settings", settings)

    def _prepare(self, d, allow_val_change=None, ignore_locked=False):
        allowed = allow_val_change if allow_val_change is not None else bool(
            (self._settings and self._settings.allow_val_change) or in_notebook())
        values = {}
        for key, value in parse_config(d).items():
            if not isinstance(key, str):
                raise TypeError("Config keys must be strings")
            key = key.strip("-")
            if key in self._locked and not ignore_locked:
                warnings.warn(f"Config item {key!r} was locked by {self._locked[key]!r}; update ignored", RuntimeWarning)
                continue
            value = friendly(value)
            if self._owner is not None:
                value = self._owner._resolve_config_artifact(value)
            if not allowed and key in self._data and self._data[key] != value:
                raise ConfigError(
                    f'Attempted to change value of key "{key}" '
                    f"from {self._data[key]} to {value}\n"
                    "If you really want to do this, pass"
                    " allow_val_change=True to config.update()"
                )
            values[key] = value
        return values

    def _apply(self, values):
        if self._owner is not None:
            self._owner._write_mapping("config", values)
        self._data.update(values)

    def update(self, d, allow_val_change=None):
        self._apply(self._prepare(d, allow_val_change))

    def persist(self):
        if self._owner is not None:
            self._owner._write_mapping("config", self._data)

    def setdefaults(self, d):
        values = {key: value for key, value in parse_config(d).items() if key.strip("-") not in self._data}
        self.update(values)

    def update_locked(self, d, user=None, _allow_val_change=None):
        values = self._prepare(d, _allow_val_change, ignore_locked=True)
        self._apply(values)
        self._locked.update({key: user for key in values})

    def merge_locked(self, d, user=None, _allow_val_change=None):
        def merge(left, right):
            result = copy.deepcopy(left)
            for key, value in right.items():
                result[key] = merge(result[key], value) if key in result and isinstance(result[key], dict) and isinstance(value, dict) else value
            return result
        values = self._prepare(d, _allow_val_change, ignore_locked=True)
        values = {key: merge(self._data[key], value) if key in self._data and isinstance(self._data[key], dict) and isinstance(value, dict) else value for key, value in values.items()}
        self._apply(values)
        self._locked.update({key: user for key in values})

    def keys(self):
        return [key for key in self._data if not key.startswith("_")]

    def items(self):
        return [(key, value) for key, value in self._data.items() if not key.startswith("_")]

    def as_dict(self):
        return dict(self.items())

    def _as_dict(self):
        return self._data

    def get(self, *args):
        return self._data.get(*args)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __contains__(self, key):
        return key in self._data

    def __setitem__(self, key, value):
        self.update({key: value})

    __setattr__ = __setitem__

    def __getattr__(self, key):
        try:
            return self._data[key]
        except KeyError as error:
            raise AttributeError(key) from error

    def __repr__(self):
        return str(dict(self))


class ConfigStatic:
    """Detached snapshot preventing top-level mutation, matching W&B ConfigStatic."""

    def __init__(self, config):
        object.__setattr__(self, "__dict__", dict(config))

    def __setattr__(self, name, value):
        raise AttributeError("run.config_static is a readonly object")

    def __setitem__(self, key, value):
        raise AttributeError("run.config_static is a readonly object")

    def __getitem__(self, key):
        return self.__dict__[key]

    def keys(self):
        return self.__dict__.keys()

    def __str__(self):
        return str(self.__dict__)
