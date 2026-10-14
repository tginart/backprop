"""Filesystem listings -> safe source names -> atomic local event snapshots.

Architecture: local readers or installed cloud SDKs list and read sources;
bytes are frozen into one local snapshot; the genuine event loader and
ordinary Run.save consume that same snapshot. Main caller: reader.sync.
Why this exists: separately decoding and uploading a changing remote source
can record two different byte streams, and partial downloads must never replace
a previously complete snapshot. Cloud clients are read-only and scoped to one
operation; no credential store or uploader is created. Missing SDKs and provider
read failures propagate.
"""
from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path, PureWindowsPath
import tempfile


def gfile():
    from tensorboard.compat import tf
    return tf.io.gfile


def filesystem(path):
    """Select stable cloud SDK readers while leaving local gfile unchanged.

    Main callers: reader._files and snapshot. Cloud support should not change
    when a training framework installs or removes its filesystem plugins.
    """
    if "://" in path:
        from .cloud import CloudFileSystem
        return CloudFileSystem(path)
    return gfile()


@contextmanager
def open_filesystem(path):
    """Release only the cloud SDK adapter created for one source operation.

    Main callers: reader._files and snapshot. Framework gfile modules have no
    owned session here; cloud adapters close their SDK HTTP/credential sessions
    even when listings or downloads fail.
    """
    selected = filesystem(path)
    try:
        yield selected
    finally:
        from .cloud import CloudFileSystem
        if isinstance(selected, CloudFileSystem):
            selected.close()


def relative_source(path, root):
    """Retain source-relative names while rejecting traversal and wrong roots.

    Main callers: reader.sync and snapshot. Cloud object keys are not local
    paths; validate components before using them as local snapshot destinations.
    A literal encoded object name stays encoded, preserving provider identity.
    Reject Windows anchors even on POSIX so a cloud name cannot escape another
    platform's cache through a drive prefix or a backslash separator.
    """
    if "://" in root:
        prefix = root.rstrip("/") + "/"
        if not path.startswith(prefix):
            raise ValueError("TensorBoard source does not belong to its metric root")
        relative = path[len(prefix):]
        if "\\" in relative or PureWindowsPath(relative).anchor or any(PureWindowsPath(part).drive for part in relative.split("/")):
            raise ValueError("TensorBoard cloud source cannot be represented as a safe relative snapshot path")
    else:
        relative = Path(path).relative_to(root).as_posix()
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")) or "\0" in relative:
        raise ValueError("TensorBoard source path contains an unsafe component")
    return relative


def snapshot(run, path, root):
    """Freeze one source read before decoding or publishing its groups.

    Main caller: reader.sync. Both local and cloud writers can append while a
    scan runs. Copy to a temporary sibling and then
    atomically replace the cached snapshot. Any provider/read failure leaves
    the prior complete snapshot untouched and removes the incomplete sibling.
    """
    cache = Path(run.sync_dir) / "tmp" / "tensorboard-sources" / hashlib.sha256(root.encode()).hexdigest()
    local = cache / relative_source(path, root)
    local.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".tensorboard-copy-", dir=local.parent, delete=False) as destination:
        temporary = Path(destination.name)
        try:
            with ExitStack() as stack:
                if "://" in path:
                    selected = stack.enter_context(open_filesystem(path))
                    source = stack.enter_context(selected.GFile(path, "rb"))
                else:
                    source = stack.enter_context(Path(path).open("rb"))
                while chunk := source.read(1024 * 1024):
                    if not isinstance(chunk, bytes):
                        raise TypeError("TensorBoard binary filesystem reads must return bytes")
                    destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, local)
    finally:
        temporary.unlink(missing_ok=True)
    return str(local), str(cache)
