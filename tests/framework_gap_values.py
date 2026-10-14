"""Freeze genuine SDK callback values into deterministic comparison records.

Architecture: real media/tables/chart specs -> decoded pixels and complete
public declarations -> recursive JSON values. Main callers: framework_gap_probe
and its dependency-independent chart regression test. Local filenames are not
values; numerical contents, captions and every chart spec field remain visible.
"""
from dataclasses import asdict
import hashlib
from pathlib import Path

import numpy as np


def portable(value, sdk):
    """Keep actual callback outputs while removing random media file identities.

    Main callers: the logging recorder and each training observation.
    Why this exists: chart titles belong to CustomChartSpec.string_fields. The
    whole dataclass is recorded so a new field or incorrect title is detected.
    Backprop's null custom_spec extension carries no extra rendering behavior;
    a non-null custom definition is retained and must pass the comparison.
    """
    if isinstance(value, sdk.Image):
        pixels = value.image.convert('RGBA')
        metadata = getattr(value, 'metadata', {})
        return {'image': list(pixels.size), 'pixels': hashlib.sha256(pixels.tobytes()).hexdigest(),
                'caption': getattr(value, '_caption', metadata.get('caption')),
                'grouping': getattr(value, '_grouping', metadata.get('grouping'))}
    if isinstance(value, sdk.Table):
        return {'columns': portable(value.columns, sdk), 'data': portable(value.data, sdk)}
    if type(value).__name__ in {'Summary', 'SummarySubDict'}:
        return {key: portable(value[key], sdk) for key in value.keys()}
    if hasattr(value, 'table') and hasattr(value, 'spec'):
        spec = asdict(value.spec)
        if spec.get('custom_spec') is None:
            spec.pop('custom_spec', None)
        return {'table': portable(value.table, sdk), 'spec': portable(spec, sdk)}
    if isinstance(value, dict):
        return {str(k): portable(v, sdk) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [portable(v, sdk) for v in value]
    if isinstance(value, np.ndarray):
        return portable(value.tolist(), sdk)
    if isinstance(value, np.generic):
        return portable(value.item(), sdk)
    if hasattr(value, 'detach'):
        return portable(value.detach().cpu().numpy(), sdk)
    if isinstance(value, Path):
        return value.name
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return type(value).__name__
