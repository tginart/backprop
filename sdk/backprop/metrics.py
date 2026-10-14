"""Metric declarations and deterministic summary reduction.

Architecture: validated definition -> persisted merged rule -> each committed
history row updates per-leaf statistics -> summary mirrors the nested key tree.
Main callers: Run.define_metric, Run._commit and run-history replay on resume.
"""
from __future__ import annotations

import copy
import math
import warnings

from .errors import Error
from .scalar_values import numeric_value


class Metric:
    def __init__(self, name, step_metric=None, step_sync=None, hidden=None, summary=None, overwrite=None):
        self._name = name
        self._step_metric = step_metric
        self._step_sync = step_sync if step_sync is not None else step_metric is not None
        self._hidden = hidden
        self._summary = summary
        self._overwrite = overwrite

    name = property(lambda self: self._name)
    step_metric = property(lambda self: self._step_metric)
    step_sync = property(lambda self: self._step_sync)
    hidden = property(lambda self: self._hidden)
    summary = property(lambda self: self._summary)


def leaves(mapping, prefix=()):
    for key, value in mapping.items():
        path = prefix + (key,)
        if isinstance(value, dict) and "_type" not in value:
            yield from leaves(value, path)
        else:
            yield path, value


def set_path(mapping, path, value):
    target = mapping
    for key in path[:-1]:
        if not isinstance(target.get(key), dict) or "_type" in target[key]:
            target[key] = {}
        target = target[key]
    target[path[-1]] = value


def delete_path(mapping, path):
    target = mapping
    for key in path[:-1]:
        target = target.get(key)
        if not isinstance(target, dict):
            return
    target.pop(path[-1], None)


def mean_value(total, count):
    if not count:
        return float("nan")
    try:
        return total / count
    except OverflowError:
        return float("inf") if total > 0 else -float("inf")


class MetricReducer:
    def __init__(self):
        self.definitions = {}
        self.last = {}
        self.last_paths = {}
        self.stats = {}
        self.explicit = set()

    def definition(self, key):
        matches = [(name, item) for name, item in self.definitions.items()
                   if name == key or (name.endswith("*") and key.startswith(name[:-1]))]
        result = {}
        for name, item in sorted(matches, key=lambda pair: (pair[0] == key, len(pair[0]))):
            result.update(item)
        return result

    def prepare_definition(self, name, step_metric=None, step_sync=None, hidden=None, summary=None, overwrite=None):
        """Return persisted merged rules and an immutable object for this call.

        Main callers: Run.define_metric; no state changes before journal append.
        """
        if isinstance(step_metric, Metric):
            step_metric = step_metric.name
        for field, value, kind in [("name", name, str), ("step_metric", step_metric, str),
                                   ("step_sync", step_sync, bool), ("hidden", hidden, bool),
                                   ("summary", summary, str), ("overwrite", overwrite, bool)]:
            if value is not None and not isinstance(value, kind):
                raise Error(f"Unhandled define_metric argument: {field} type: {type(value).__name__}")
        if not name or "*" in (name[:-1] if name.endswith("*") else name):
            raise Error("define_metric requires a nonempty name with only a trailing * wildcard")
        ops = [x.lower() for x in summary.split(",")] if summary else None
        valid = {"min", "max", "mean", "first", "last", "copy", "none"}
        if ops and not set(ops) <= valid:
            raise Error("Unsupported metric summary aggregation")
        if ops and "copy" in ops:
            warnings.warn("define_metric(summary='copy') is deprecated", DeprecationWarning, stacklevel=3)
        metric = Metric(name, step_metric, step_sync, hidden, tuple(ops) if ops is not None else None, overwrite)
        definition = {} if overwrite else dict(self.definitions.get(name, {}))
        definition["name"] = name
        if step_metric is not None:
            definition["step_metric"] = step_metric
        # The pinned service combines booleans and aggregate flags unless
        # overwrite=True clears the prior record first.
        if step_sync is not None or step_metric is not None:
            definition["step_sync"] = definition.get("step_sync", False) or metric.step_sync
        if hidden is not None:
            definition["hidden"] = definition.get("hidden", False) or hidden
        if ops is not None:
            prior = definition.get("summary", "").split(",") if not overwrite else []
            combined = list(dict.fromkeys(x for x in [*prior, *ops] if x and x not in {"copy", "none"}))
            definition["summary"] = "none" if "none" in ops or "none" in prior else ",".join(combined) or "copy"
        if overwrite:
            definition["overwrite"] = True
        return definition, metric

    def synchronize(self, values):
        result = copy.deepcopy(values)
        present = {".".join(path) for path, _ in leaves(values)}
        for path, value in leaves(values):
            key = ".".join(path)
            rule = self.definition(key)
            axis = rule.get("step_metric")
            if axis and rule.get("step_sync", True) and axis not in present and axis in self.last:
                set_path(result, self.last_paths.get(axis, (axis,)), copy.deepcopy(self.last[axis]))
        return result

    def reconfigure(self, name):
        """A new metric declaration restores its configured summary tracking."""
        self.explicit = {path for path in self.explicit if not (
            ".".join(path) == name or (name.endswith("*") and ".".join(path).startswith(name[:-1])))}

    def explicit_update(self, values, deleted):
        for path, value in leaves(values):
            self.explicit.add(path)
        for key in deleted:
            self.explicit = {p for p in self.explicit if p[0] != key}
            for path, stats in self.stats.items():
                if path[0] == key:
                    stats["reset_first"] = True

    def apply(self, metrics, summary):
        """Reduce a committed history row with per-leaf numeric statistics."""
        for path, value in leaves(metrics):
            key = ".".join(path)
            self.last[key] = copy.deepcopy(value)
            self.last_paths[key] = path
            rule = self.definition(key)
            ops = rule.get("summary", "copy").split(",")
            if "none" in ops:
                delete_path(summary, path)
                continue
            stats = self.stats.setdefault(path, {"first": value, "last": value, "count": 0, "sum": 0, "min": 0, "max": 0})
            if stats.pop("reset_first", False):
                stats["first"] = value
            stats["last"] = value
            numeric = numeric_value(value)
            if numeric is not None:
                # Go's builtin min/max propagate NaN and distinguish signed
                # zero. Python min/max alone have different NaN semantics.
                for operation in ("min", "max"):
                    old = stats[operation]
                    if not stats["count"]:
                        result = numeric
                    elif isinstance(old, float) and math.isnan(old) or isinstance(numeric, float) and math.isnan(numeric):
                        result = float("nan")
                    elif old == 0 and numeric == 0:
                        signs = (math.copysign(1, old), math.copysign(1, numeric))
                        result = math.copysign(0.0, min(signs) if operation == "min" else max(signs))
                    else:
                        result = min(old, numeric) if operation == "min" else max(old, numeric)
                    stats[operation] = result
                try:
                    stats["sum"] += numeric
                except OverflowError:
                    stats["sum"] = float("inf") if (numeric if isinstance(numeric, int) else stats["sum"]) > 0 else -float("inf")
                stats["count"] += 1
            if path in self.explicit or ops == ["copy"] or not ops:
                result = value
            else:
                result = {op: mean_value(stats["sum"], stats["count"]) if op == "mean" else stats[op]
                          for op in ops if op != "copy"}
                if not result:
                    result = value
            set_path(summary, path, result)
