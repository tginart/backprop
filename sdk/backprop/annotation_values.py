"""Validation and temporary files for portable image annotations.

Architecture: pinned public input checks -> file bytes frozen at construction ->
shared Media binding -> existing run journals and artifact manifests. Native
image descriptors carry labels inline so the browser needs no singleton service.
Main callers: BoundingBoxes2D and ImageMask in media.py. Algorithms follow the
MIT-licensed W&B 0.30.0 helpers; see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import json
import numbers
from pathlib import Path
import sys
import tempfile
import threading
import uuid

_temporary = None
_temporary_lock = threading.Lock()


def temporary_path(extension):
    """Own annotation scratch files until they move into a run or process exits.

    Main callers: annotation constructors. This is process-local scratch space,
    not persistent storage; durable bytes use the ordinary journal/file paths.
    """
    global _temporary
    with _temporary_lock:
        if _temporary is None:
            _temporary = tempfile.TemporaryDirectory(prefix='backprop-annotations-')
    return Path(_temporary.name) / (uuid.uuid4().hex + extension)


def required_module(name, message):
    """Keep the pinned public Error for unavailable annotation encoders."""
    from .tables import _optional_module
    from .errors import Error
    try:
        return _optional_module(name, message)
    except ImportError as error:
        raise Error(message) from error


def is_torch_tensor(value):
    name = type(value).__module__ + '.' + type(value).__name__
    return name.startswith('torch.') and ('Tensor' in name or 'Variable' in name)


def prepare_boxes(val):
    """Normalize the pinned Tensor center form in the caller's box dictionary."""
    for box in val.get('box_data', []):
        if 'position' in box and 'middle' in box['position'] and is_torch_tensor(box['position']['middle']):
            box['position']['middle'] = box['position']['middle'].tolist()


def validate_labels(labels, message):
    for key, value in list(labels.items()):
        if not isinstance(key, numbers.Number) or not isinstance(value, str):
            raise TypeError(message)


def validate_boxes(val):
    """Match public BoundingBoxes2D.validate, including permissive center data.

    Main callers: explicit validate and construction. Rendering rejects invalid
    geometry separately; validation must not silently tighten the pinned API.
    """
    if 'class_labels' in val:
        validate_labels(val['class_labels'], 'Class labels must be a dictionary of numbers to string')
    boxes = val['box_data']
    if not isinstance(boxes, list):
        raise TypeError('Boxes must be a list')
    def has_number(position, key):
        return key in position and isinstance(position[key], numbers.Number)
    for box in boxes:
        error = 'Each box must contain a position with: middle, width, and height or                     \nminX, maxX, minY, maxY.'
        if 'position' not in box:
            raise TypeError(error)
        position = box['position']
        center = ('middle' in position and len(position['middle']) == 2
                  and has_number(position, 'width') and has_number(position, 'height'))
        corners = all(has_number(position, key) for key in ['minX','maxX','minY','maxY'])
        if not center and not corners:
            raise TypeError(error)
        if 'scores' in box and not isinstance(box['scores'], dict):
            raise TypeError('Box scores must be a dictionary')
        elif 'scores' in box:
            for key,value in list(box['scores'].items()):
                if not isinstance(key,str): raise TypeError('A score key must be a string')
                if not isinstance(value,numbers.Number): raise TypeError('A score value must be a number')
        if 'class_id' in box and not isinstance(box['class_id'],int):
            raise TypeError("A box's class_id must be an integer")
        if 'box_caption' in box and not isinstance(box['box_caption'],str):
            raise TypeError("A box's caption must be a string")
    return True


def validate_mask(val):
    """Match the reference shape/range check; fractional values are cast at save.

    Main callers: explicit ImageMask.validate and its array constructor. Local
    path inputs bypass this array validator, just as in the pinned SDK.
    """
    required_module('numpy', 'Image mask support requires numpy')
    if 'mask_data' not in val:
        raise TypeError('Missing key "mask_data": An image mask requires mask data: a 2D array representing the predictions')
    shape=val['mask_data'].shape
    if len(shape)!=2: raise TypeError('mask_data must be a 2D array')
    if not ((val['mask_data']>=0).all() and (val['mask_data']<=255).all()):
        raise TypeError('Mask data must be integers between 0 and 255')
    if 'class_labels' in val:
        validate_labels(val['class_labels'], 'Class labels must be a dictionary of numbers to strings')
    return True


def write_boxes(val):
    """Freeze constructor JSON with the pinned NumPy conversion and spacing."""
    def default(value):
        numpy=sys.modules.get('numpy')
        if numpy is not None and isinstance(value,(numpy.ndarray,numpy.generic)):
            return value.tolist() if isinstance(value,numpy.ndarray) else value.item()
        raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')
    path=temporary_path('.boxes2D.json')
    import pydantic_core
    try:
        data = pydantic_core.to_json(val, inf_nan_mode="constants", fallback=default)
    except Exception:
        # The pinned serializer retries unsupported values with stdlib JSON,
        # preserving its public TypeError behavior for invalid extra fields.
        data = json.dumps(val, default=default).encode("utf-8")
    path.write_bytes(data)
    return path
