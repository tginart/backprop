"""Normalize Plotly payloads before ordinary metric encoding can see them.

Architecture: Figure.to_plotly_json -> nested arrays as lists -> JSON snapshot.
The pinned W&B wrapper uses this sequence rather than Figure.to_json. In
particular, a long coordinate array must remain coordinates, never a histogram.
Main callers: media.Plotly, including native dictionary-spec reconstruction.
Optional plotting/numerical libraries are used only when already needed by the
input. The serializer and matplotlib spine shim follow the pinned MIT source.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
import enum
import json
import sys


def _arrays_to_lists(value):
    """Preserve the pinned recursive ndarray, sequence and media conversion."""
    from .media import Media
    numpy = sys.modules.get("numpy")
    if isinstance(value, dict):
        return {key: _arrays_to_lists(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_arrays_to_lists(item) for item in value]
    if numpy is not None and isinstance(value, numpy.ndarray):
        items = value.tolist() if value.ndim > 0 else [value.tolist()]
        return [_arrays_to_lists(item) for item in items]
    if isinstance(value, Media):
        return type(value).__name__
    return value


def _tensor_value(value):
    """Apply pinned tensor conversion only to recognized library type names.

    Main callers: the JSON encoder. These tensors are plot metadata or custom
    data, so metric encoding (and its histogram conversion) must not run here.
    """
    typename = type(value).__module__ + "." + type(value).__name__
    if typename.startswith("tensorflow.") and "EagerTensor" in typename:
        return value.numpy()
    if typename.startswith("tensorflow.") and ("Tensor" in typename or "Variable" in typename):
        try:
            return value.eval()
        except RuntimeError:
            return value.numpy()
    if (typename.startswith("torch.") and ("Tensor" in typename or "Variable" in typename)
            or typename.startswith("fastai.") and "Tensor" in typename):
        try:
            if value.requires_grad:
                value = value.detach()
        except AttributeError:
            pass
        try:
            value = value.data
        except RuntimeError:
            pass
        return value.cpu().detach().numpy() if value.size() else value.item()
    if typename.startswith("jaxlib.") and "Array" in typename:
        import jax
        return jax.device_get(value)
    return value


class _PlotJSONEncoder(json.JSONEncoder):
    """Snapshot Plotly's dates/scalars without producing native rich cells."""

    def default(self, value):
        if hasattr(value, "json_encode"):
            return value.json_encode()
        original = value
        value = _tensor_value(value)
        numpy = sys.modules.get("numpy")
        if numpy is not None and isinstance(value, numpy.ndarray):
            if value.size == 1:
                return value.flatten()[0]
            if value.size <= 32:
                return value.tolist()
            return value
        if numpy is not None and isinstance(value, numpy.generic):
            result = value.item()
            if isinstance(result, numpy.generic) and (result.dtype.kind == "f" or result.dtype == "bfloat16"):
                return float(result)
            return result
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if callable(value):
            return (f"{value.__module__}.{value.__qualname__}"
                    if hasattr(value, "__module__") and hasattr(value, "__qualname__") else str(value))
        if isinstance(value, set):
            return tuple(value)
        if isinstance(value, enum.Enum):
            return value.name
        if value is not original:
            return value
        return super().default(value)


def snapshot_bytes(value):
    """Encode the shared pinned JSON format used by Plotly and Table files.

    Main callers: snapshot_spec and Table.bind_to_run. Conversion happens before
    file publication. The Pydantic-first encoder is part of W&B 0.30.0's public
    file behavior, including byte formatting and errors on unknown values.
    """
    import pydantic_core
    prepared = _arrays_to_lists(value)
    try:
        return pydantic_core.to_json(prepared, inf_nan_mode="constants", fallback=_PlotJSONEncoder().default)
    except Exception:
        return json.dumps(prepared, cls=_PlotJSONEncoder).encode("utf-8")


def snapshot_spec(value):
    """Detach supported figure JSON from subsequent caller mutations."""
    return json.loads(snapshot_bytes(value))


def prepare_matplotlib_conversion():
    """Restore the spine method expected by Plotly's matplotlib converter.

    Main callers: Plotly.__init__ after selecting a genuine matplotlib figure.
    Why this exists: matplotlib removed this method; pinned W&B supplies the
    same shim so installed Plotly versions can still inspect ordinary axes.
    """
    from matplotlib.spines import Spine

    def is_frame_like(self):
        position = self._position or ("outward", 0.0)
        if isinstance(position, str):
            if position == "center":
                position = ("axes", 0.5)
            elif position == "zero":
                position = ("data", 0)
        if len(position) != 2:
            raise ValueError("position should be 2-tuple")
        position_type, amount = position
        return bool(position_type == "outward" and amount == 0)

    Spine.is_frame_like = is_frame_like
