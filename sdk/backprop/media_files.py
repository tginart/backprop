"""Process-owned scratch files for public encoded media values.

Architecture: construction freezes encoded bytes into a temporary file; Media
binding moves that file into a run; existing journals/artifacts own durable
copies. Process exit cleans up unbound files. No new storage service is used.
Main callers: Image, Audio, Video, Html and Plotly constructors in media.py.
"""
from pathlib import Path
import tempfile
import threading
import uuid

_directory = None
_lock = threading.Lock()


def freeze_file(data, extension):
    """Freeze bytes once and retain their path until binding or process exit.

    Main callers: generated-media construction. A real source file lets the shared
    Media lifecycle apply naming, copying, linking and ownership consistently.
    """
    global _directory
    with _lock:
        if _directory is None:
            _directory = tempfile.TemporaryDirectory(prefix="backprop-media-")
    path = Path(_directory.name) / (uuid.uuid4().hex + extension)
    path.write_bytes(data)
    return str(path)
