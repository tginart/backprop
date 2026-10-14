"""Keep synchronized training axes separate from immutable source observations.

Architecture: compare pre/post metric synchronization -> record added leaf paths
-> validate non-overlap with source values -> overlay the selected owner's axes.
Main callers: Run._commit and TensorBoard source projection. Source revisions
may replace earlier imported values; an injected axis can never replace source
data. When multiple importers disagree, the earliest surviving owner supplies
the supplementary axes, matching its preserved writer and local step.
"""
import copy
from .metrics import leaves, set_path


def step_sync_annotations(original, synchronized):
    before = {path for path, _ in leaves(original)}
    return [{'path': list(path), 'value': copy.deepcopy(value)} for path, value in leaves(synchronized)
            if path not in before]


def apply_step_sync(source, annotations, *, converged=False):
    """Validate declared axes and return a copy with non-source values added.

    Main callers: group validation (strict) and union projection (converged).
    A later source revision can introduce an axis itself; its value then wins.
    """
    if not isinstance(annotations, list): raise ValueError('TensorBoard step-sync annotations must be a list')
    # Source-derived values may alias their copy inside the group sidecar.
    # Clone roots independently so adding an axis cannot edit member evidence.
    result = {key: copy.deepcopy(value) for key, value in source.items()}
    source_paths = [path for path, _ in leaves(source)]
    seen = []
    for item in annotations:
        if not isinstance(item, dict) or set(item) != {'path', 'value'}:
            raise ValueError('Invalid TensorBoard step-sync annotation')
        path = item['path']
        if not isinstance(path, list) or not path or any(not isinstance(key, str) for key in path) or path[0].startswith('_tensorboard'):
            raise ValueError('Invalid TensorBoard step-sync path')
        path = tuple(path)
        overlap = lambda other: path[:len(other)] == other or other[:len(path)] == path
        if any(overlap(previous) for previous in seen): raise ValueError('Overlapping TensorBoard step-sync paths')
        seen.append(path)
        if any(overlap(original) for original in source_paths):
            if converged: continue
            raise ValueError('TensorBoard step-sync annotation overwrites source evidence')
        set_path(result, path, copy.deepcopy(item['value']))
    return result
