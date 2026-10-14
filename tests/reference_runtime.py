"""Resolve a pinned reference interpreter without mixing its dependencies.

Architecture: explicit interpreter or current local Python -> isolated metadata
query -> checked W&B version/site paths -> caller-owned reference subprocess.
Main callers: init comparison tests; other framework owners may adopt this
boundary independently. Why this exists: selecting reference site-packages on
native PYTHONPATH shares incompatible TensorFlow/protobuf runtimes. A configured
missing interpreter or wrong pin is an error, never an optional skip.
"""
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess
import sys


@dataclass(frozen=True)
class ReferenceRuntime:
    """Checked metadata for one actual pinned reference Python process."""
    python: str
    version: str
    site_root: str
    protobuf_version: str


@lru_cache(maxsize=None)
def _resolve(python):
    """Query metadata through that interpreter's own isolated import paths.

    Main caller: reference_runtime. Python's -I excludes inherited PYTHONPATH,
    and this query imports no SDK/service or framework. Original child failures
    are retained in the error rather than substituted from native metadata.
    """
    script = ("import importlib.metadata as m,json; d=m.distribution('wandb'); "
              "print(json.dumps({'version':d.version,'site_root':str(d.locate_file('')),"
              "'protobuf_version':m.version('protobuf')}))")
    result = subprocess.run([python, '-I', '-c', script], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Reference interpreter metadata query failed: ' + result.stderr)
    data = json.loads(result.stdout)
    if data['version'] != '0.30.0':
        raise RuntimeError('Reference interpreter must contain W&B0.30.0; found ' + data['version'])
    return ReferenceRuntime(python, **data)


def reference_runtime():
    """Honor explicit reference selection or the existing local interpreter.

    Main callers: paired init tests. Callers may skip a locally absent optional
    wheel, but explicit CI selection must remain a checked requirement.
    Preserve a virtualenv executable's symlink path: resolving it to the base
    executable would select the base interpreter's dependency environment.
    """
    configured = os.environ.get('BACKPROP_REFERENCE_PYTHON')
    python = str(Path(configured).expanduser().absolute()) if configured else sys.executable
    return _resolve(python)
