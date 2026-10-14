"""Nested summary views that persist edits at their containing root key.

Architecture: Summary/SubDict keep a root/path view -> mutations copy a subtree
-> owner validates and journals -> local state commits. Existing event consumers
still receive ordinary top-level values/deleted updates.
Main callers: run.summary, module summary alias and automatic metric reducers.
"""
from __future__ import annotations

import copy
import io
from .scalar_values import decode_numbers


class SummaryItem:
    def __init__(self):
        self.key, self.value = (), None

    def _add_next_parent(self, parent_key):
        result = SummaryItem()
        result.key = (parent_key,) + tuple(self.key)
        result.value = self.value
        return result


class SummaryRecord:
    def __init__(self):
        self.update, self.remove = [], []

    def _add_next_parent(self, parent_key):
        result = SummaryRecord()
        result.update = [item._add_next_parent(parent_key) for item in self.update]
        result.remove = [item._add_next_parent(parent_key) for item in self.remove]
        return result


def prepare_summary(value, stage_file):
    """Persist large arrays losslessly and return the official numeric statistics.

    Main callers: Run._write_mapping for explicit summary assignments. NumPy
    is imported only for array/tensor inputs, so ordinary logging stays stdlib.
    Binary NPY storage replaces W&B's HDF5 container while preserving dtype and
    shape; the descriptor retains the original array/tensor type and statistics.
    """
    if isinstance(value, dict):
        return {key: prepare_summary(item, stage_file) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [prepare_summary(item, stage_file) for item in value]
    module = type(value).__module__.split(".")[0]
    if module not in {"numpy", "torch", "tensorflow", "jax", "jaxlib"}:
        return value
    import numpy as np
    if hasattr(value, "detach"):
        array = value.detach().cpu().numpy()
    elif hasattr(value, "numpy"):
        array = value.numpy()
    else:
        array = np.asarray(value)
    if array.size <= 32:
        return array.tolist()
    statistics = {"_type": "torch.Tensor" if module == "torch" else "tensorflow.Tensor" if module == "tensorflow" else "numpy.ndarray",
                  "var": np.var(array).item(), "mean": np.mean(array).item(),
                  "min": np.amin(array).item(), "max": np.amax(array).item(),
                  **{f"{percent}%": np.percentile(array, percent).item() for percent in (10, 25, 75, 90)},
                  "size": int(array.size), "shape": list(array.shape), "dtype": str(array.dtype)}
    output = io.BytesIO()
    np.save(output, array, allow_pickle=False)
    reference = stage_file(None, data=output.getvalue(), name="summary-array.npy")
    return {**statistics, "file": {**reference, "mime_type": "application/octet-stream"}}


class Summary:
    def __init__(self, get_current_summary_callback=None):
        callback = get_current_summary_callback if callable(get_current_summary_callback) else None
        object.__setattr__(self, "_owner", None if callback else get_current_summary_callback)
        object.__setattr__(self, "_get_current_summary_callback", callback)
        object.__setattr__(self, "_update_callback", None)
        object.__setattr__(self, "_data", {})
        object.__setattr__(self, "_path", ())
        object.__setattr__(self, "_root", self)

    def _as_dict(self):
        root = self._root
        if root._owner is not None and root._owner._closed and not root._owner.disabled:
            root._owner._warn("Summary data not available in finished run")
            return {}
        value = root._get_current_summary_callback() if root._get_current_summary_callback else root._data
        for key in self._path:
            value = value[key]
        return decode_numbers(value)

    def _set_update_callback(self, update_callback):
        object.__setattr__(self, "_update_callback", update_callback)

    def _update(self, record):
        if self._root._update_callback:
            return self._root._update_callback(record)

    def as_dict(self):
        return copy.deepcopy(self._as_dict())

    def keys(self):
        return [key for key in self._as_dict() if key != "_wandb"]

    def get(self, key, default=None):
        return self._as_dict().get(key, default)

    def __getitem__(self, key):
        value = self._as_dict()[key]
        if isinstance(value, dict):
            return SummarySubDict(self._root, self._path + (key,))
        return value

    __getattr__ = __getitem__

    def __setitem__(self, key, value):
        self.update({key: value})

    __setattr__ = __setitem__

    def update(self, d):
        # The public contract consumes items(), including mapping proxies and
        # custom mapping objects, before publishing any part of the update.
        self._mutate(dict(d.items()), [])

    def __delitem__(self, key):
        self._mutate({}, [key])

    __delattr__ = __delitem__

    def _mutate(self, values, deleted):
        """Publish one root-key update even when callers edit deeply nested data.

        Main callers: item/attribute assignment, deletion, and update(d).
        """
        if not isinstance(values, dict):
            raise TypeError("Summary.update expects a dictionary")
        if any(not isinstance(key, str) for key in [*values, *deleted]):
            raise TypeError("Summary keys must be strings")
        root = self._root
        if root._get_current_summary_callback is not None:
            record = SummaryRecord()
            for key, value in values.items():
                item = SummaryItem()
                item.key, item.value = self._path + (key,), value
                record.update.append(item)
            for key in deleted:
                item = SummaryItem()
                item.key = self._path + (key,)
                record.remove.append(item)
            self._update(record)
            return
        if self._path:
            first = self._path[0]
            subtree = copy.deepcopy(root._data[first])
            target = subtree
            for key in self._path[1:]:
                target = target[key]
            target.update(values)
            for key in deleted:
                target.pop(key, None)
            values, deleted = {first: subtree}, []
        if root._owner is not None:
            values = root._owner._write_mapping("summary", values, deleted)
        root._data.update(values)
        for key in deleted:
            root._data.pop(key, None)

    def __repr__(self):
        return repr(self._as_dict())

    def __eq__(self, other):
        return self._as_dict() == (other._as_dict() if isinstance(other, Summary) else other)


class SummarySubDict(Summary):
    def __init__(self, root=None, path=()):
        if root is None:
            super().__init__()
            object.__setattr__(self, "_items", {})
            object.__setattr__(self, "_parent", None)
            object.__setattr__(self, "_parent_key", None)
            object.__setattr__(self, "_unbound", True)
            return
        object.__setattr__(self, "_root", root)
        object.__setattr__(self, "_path", tuple(path))

    def _as_dict(self):
        return self._items if self.__dict__.get("_unbound") else super()._as_dict()

    def _mutate(self, values, deleted):
        if not self.__dict__.get("_unbound"):
            return super()._mutate(values, deleted)
        record = SummaryRecord()
        for key, value in values.items():
            item = SummaryItem()
            item.key, item.value = (key,), value
            record.update.append(item)
        for key in deleted:
            item = SummaryItem()
            item.key = (key,)
            record.remove.append(item)
        return self._parent._update(record._add_next_parent(self._parent_key))
