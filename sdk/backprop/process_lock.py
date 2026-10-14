"""Serialize durable file operations across processes using an open lock file.

Architecture: caller owns a stable lock inode -> acquire OS lock -> perform the
protected operation -> always release. Main callers: credential replacement and
artifact handoff preparation/upload. Callers choose file permissions separately.
"""
from contextlib import contextmanager
import os


@contextmanager
def exclusive_lock(lock):
    """Hold an OS-level lock across an operation and its durable acknowledgement.

    Main callers: credential_store._locked and artifact_handoff.publish_pending.
    A stable lock file survives replacement of the protected data files.
    """
    if os.name == 'nt':
        import msvcrt
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b'\0'); lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        yield
    finally:
        if os.name == 'nt':
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
