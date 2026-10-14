"""Keep Foundry credentials separate from W&B files and experiment journals.

Architecture: normalize the host -> read a private versioned file -> lock and
merge one host entry -> fsync and replace atomically. Readers see either complete
snapshot; concurrent writers retain each other's hosts. Nothing here contacts a
server, prints a token, or modifies a run. Main callers: login_session and Settings.
Why this exists: the selected credentials_file contract needs restart persistence
without a hosted authentication service or W&B's JWT/.netrc machinery.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile

from .errors import AuthenticationError, UsageError


def default_path():
    """Return Backprop's own default; never read or overwrite W&B's cache."""
    return str(Path.home() / '.config/backprop/credentials.json')


def _private_open(path, flags):
    """Open a regular, owner-private file without following a final symlink.

    Main callers: cache readers and the persistent write-lock file. Checking the
    opened descriptor avoids racing a replace between validation and use.
    """
    descriptor = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise AuthenticationError('Credential storage must be a regular file')
        if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise AuthenticationError('Credential storage must be owned by the current user with permissions 0600')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(path):
    """Read a complete snapshot, treating only a nonexistent file as empty."""
    try:
        descriptor = _private_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return {'version': 1, 'hosts': {}}
    except OSError:
        raise AuthenticationError('Cannot open the Foundry credential file') from None
    try:
        with os.fdopen(descriptor, 'r', encoding='utf-8') as source:
            data = json.load(source)
        if not isinstance(data, dict) or set(data) != {'version', 'hosts'} or type(data['version']) is not int or data['version'] != 1 or not isinstance(data['hosts'], dict):
            raise ValueError()
        from .login_session import _host_origin, validate_token
        for host, token in data['hosts'].items():
            if _host_origin(host).lower() != host:
                raise ValueError()
            validate_token(token)
        return data
    except (ValueError, TypeError, UnicodeError, AuthenticationError, UsageError):
        raise AuthenticationError('Invalid Foundry credential file; existing contents were preserved') from None


def read_token(path, host):
    """Select only the requested origin; no cross-host credential fallback.

    Main callers: login and online SDK factories, after higher priority tokens.
    """
    from .login_session import _host_origin
    return _read(Path(path).expanduser())['hosts'].get(_host_origin(host).lower())


@contextmanager
def _locked(path):
    """Serialize cache replacements across processes using a stable lock file.

    Main caller: write_token. The lock survives data-file replacement; using the
    data inode itself would let another writer lock an obsolete snapshot.
    """
    descriptor = _private_open(str(path) + '.lock', os.O_CREAT | os.O_RDWR)
    with os.fdopen(descriptor, 'r+b') as lock:
        from .process_lock import exclusive_lock
        with exclusive_lock(lock):
            yield


def write_token(path, host, token):
    """Atomically persist a successfully selected credential without losing hosts.

    Main caller: login publication, after validation and optional verification.
    A failure before replacement leaves the previous cache intact and is raised
    explicitly, so the caller never reports a persistent login that was lost.
    """
    from .login_session import _host_origin, validate_token
    host = _host_origin(host).lower()
    validate_token(token)
    path = Path(path).expanduser()
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _locked(path):
            data = _read(path)
            data['hosts'][host] = token
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.backprop-credentials-', delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                json.dump(data, output)
                output.write('\n'); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, path)
            temporary = None
            if os.name == 'posix':
                directory = os.open(path.parent, os.O_RDONLY)
                try: os.fsync(directory)
                finally: os.close(directory)
    except OSError:
        raise AuthenticationError('Cannot persist Foundry credentials; check the configured credential file') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
