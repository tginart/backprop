"""Public artifact constructor validation and plain descriptive metadata.

Architecture: validate a new user identity -> convert descriptive values using
W&B 0.30.0's JSON rules -> detach them through a strict JSON round trip. Logging
and descriptive edits use the same metadata conversion. Artifact restoration
bypasses public name restrictions because saved collection names can be edited.
Main callers: Artifact.__init__, artifact publication and Artifact.save.
Why this exists: metadata is ordinary JSON, not logged media. Interpreting a
user's `_type` dictionary as our event encoding changes their original data.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
import json
import math
import re
import sys

from pydantic_core import from_json


def constructor_name(name):
    """Validate a new user name before any constructor warning or allocation.

    Main caller: Artifact.__init__. Mutable collection names and persisted
    identities deliberately have a different validation boundary.
    """
    if not re.match(r"^[a-zA-Z0-9_\-.]+$", name):
        raise ValueError(
            "Artifact name may only contain alphanumeric characters, dashes, "
            f"underscores, and dots. Invalid name: {name!r}"
        )
    return name


def constructor_identity(name, artifact_type, internal=False):
    """Apply length and reserved identity checks after incremental warnings.

    Main caller: Artifact.__init__. Type strings are descriptive; the pinned
    constructor does not reject empty types, slashes or colons.
    """
    if len(name) > 128:
        raise ValueError(f"Artifact name is longer than 128 characters: {name[:128] + ' ...'!r}")
    if not internal:
        reserved = {'job': '', 'run_table': 'run-', 'code': 'source-'}
        prefix = reserved.get(artifact_type)
        if prefix is not None and name.startswith(prefix) or artifact_type.startswith('wandb-'):
            raise ValueError(f"Artifact type {artifact_type!r} is reserved for internal use. Please use a different type.")


def _scalar_or_array(value):
    """Convert framework tensors and NumPy values before recursive JSON work.

    Main caller: _friendly. Frameworks are imported only when a value from that
    framework is supplied. The pinned small-array threshold and scalar behavior
    are preserved, including string descriptions for large arrays.
    """
    typename = f'{type(value).__module__}.{type(value).__name__}'
    if typename.startswith('tensorflow.') and ('Tensor' in typename or 'Variable' in typename):
        if 'EagerTensor' in typename:
            value = value.numpy()
        else:
            try:
                value = value.eval()
            except RuntimeError:
                value = value.numpy()
    elif (typename.startswith('torch.') and ('Tensor' in typename or 'Variable' in typename)
            or typename.startswith('fastai.') and 'Tensor' in typename):
        if getattr(value, 'requires_grad', False):
            value = value.detach()
        try:
            value = value.data
        except RuntimeError:
            pass
        if not value.size():
            return value.item(), True
        value = value.cpu().detach().numpy()
    elif typename.startswith('jaxlib.') and 'Array' in typename:
        import jax
        value = jax.device_get(value)
    numpy = sys.modules.get('numpy')
    if numpy is not None and isinstance(value, numpy.ndarray):
        if value.size == 1:
            value = value.flatten()[0]
        elif value.size <= 32:
            value = value.tolist()
        return value, True
    if numpy is not None and isinstance(value, numpy.generic):
        value = value.item()
        if isinstance(value, float) and math.isnan(value):
            value = None
        elif isinstance(value, numpy.generic) and (value.dtype.kind == 'f' or value.dtype == 'bfloat16'):
            value = float(value)
        return value, True
    return value, False


def _friendly(value):
    """Recursively match the pinned descriptive JSON conversion, not media tags.

    Main caller: metadata. Unknown third-party values use their text form;
    unsupported builtin objects retain ordinary JSON serialization errors.
    """
    if isinstance(value, dict):
        return {key: _friendly(item) for key, item in value.items()}
    if isinstance(value, slice):
        return {'slice_start': value.start, 'slice_step': value.step, 'slice_stop': value.stop}
    value, converted = _scalar_or_array(value)
    if not converted:
        if isinstance(value, bytes):
            value = value.decode('utf-8')
        elif isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif callable(value):
            value = (f'{value.__module__}.{value.__qualname__}'
                     if hasattr(value, '__module__') and hasattr(value, '__qualname__') else str(value))
        elif isinstance(value, float) and math.isnan(value):
            value = None
        elif isinstance(value, set):
            value = tuple(value)
        elif isinstance(value, Enum):
            value = value.name
    if sys.getsizeof(value) > 100000:
        from . import termwarn
        termwarn(f'Serializing object of type {type(value).__name__} that is {sys.getsizeof(value)} bytes')
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_friendly(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _friendly(asdict(value))
    if value.__class__.__module__ not in ('builtins', '__builtin__'):
        return str(value)
    return value


def metadata(value):
    """Return detached descriptive JSON, enforcing the pinned top-level limit.

    Main callers: Artifact construction, content identity and descriptive edits.
    Parse JSON strings with the same parser as the pinned reference, then apply
    conversion and reject infinities before checking the 100-key limit.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        return metadata(from_json(value)) if value else {}
    if not isinstance(value, dict):
        raise TypeError(f'Cannot parse {type(value)} as artifact metadata')
    result = from_json(json.dumps(_friendly(value), allow_nan=False))
    if len(result) > 100:
        raise ValueError('Artifact must not have more than 100 metadata keys.')
    return result
