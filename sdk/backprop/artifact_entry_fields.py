"""Portable field coercion for immutable ArtifactManifestEntry objects.

Architecture: accept keyword inputs and aliases -> normalize path/scalar fields
-> infer optional local size -> construct an immutable entry. Serialization is
handled separately by artifact_entry_serialization, which uses Pydantic while
keeping native checksum/store data private.
Main caller: ArtifactManifestEntry construction.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from functools import wraps
import inspect
import numbers
import os
from pathlib import PurePath, PurePosixPath
import platform
import re


class LogicalPath(str):
    """A normalized string with PurePosixPath's non-string path operations."""
    def __new__(cls, path):
        if isinstance(path, LogicalPath):
            return super().__new__(cls, path)
        if hasattr(path, 'as_posix'):
            return super().__new__(cls, str(PurePosixPath(path.as_posix())))
        if hasattr(path, '__fspath__'):
            path = path.__fspath__()
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if platform.system() == 'Windows':
            path = path.replace('\\', '/')
        return super().__new__(cls, str(PurePosixPath(PurePath(path).as_posix())))

    def to_path(self):
        return PurePosixPath(self)

    def __getattr__(self, name):
        try:
            value = getattr(self.to_path(), name)
        except AttributeError:
            raise AttributeError(f'{type(self).__qualname__!r} has no attribute {name!r}') from None
        if isinstance(value, PurePosixPath):
            return type(self)(value)
        if callable(value):
            @wraps(value)
            def call(*args, **kwargs):
                result = value(*args, **kwargs)
                return type(self)(result) if isinstance(result, PurePosixPath) else result
            return call
        return value

    def __truediv__(self, other):
        return type(self)(self.to_path() / type(self)(other))


def _string(value, field, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode('utf-8')
        except UnicodeDecodeError as error:
            raise ValueError(f'Artifact entry {field} must be valid UTF-8') from error
    if not isinstance(value, str):
        raise ValueError(f'Artifact entry {field} must be a string')
    return str(value)


def _number(value):
    """Accept the numeric conversion protocols used by entry scalar validation."""
    if isinstance(value, (numbers.Real, Decimal)):
        return value
    if hasattr(type(value), '__float__') or hasattr(type(value), '__index__'):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('Artifact entry numeric value is invalid') from error
    raise ValueError('Artifact entry numeric value is invalid')


def _size(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8')
        except UnicodeDecodeError as error:
            raise ValueError('Artifact entry size must be a nonnegative integer') from error
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r'[+-]?[0-9](?:_?[0-9])*(?:\.0+)?', value) is None:
            raise ValueError('Artifact entry size must be a nonnegative integer')
        value = int(value.split('.')[0].replace('_', ''))
    else:
        value = _number(value)
        try:
            integer = int(value)
        except (OverflowError, ValueError) as error:
            raise ValueError('Artifact entry size must be a nonnegative integer') from error
        if integer != value:
            raise ValueError('Artifact entry size must be a nonnegative integer')
        value = integer
    if value < 0:
        raise ValueError('Artifact entry size must be a nonnegative integer')
    return value


def _boolean(value):
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8')
        except UnicodeDecodeError as error:
            raise ValueError('Artifact entry skip_cache must be boolean') from error
    if isinstance(value, str):
        if value.lower() in {'0', 'off', 'f', 'false', 'n', 'no'}:
            return False
        if value.lower() in {'1', 'on', 't', 'true', 'y', 'yes'}:
            return True
    else:
        number = _number(value)
        if number in (0, 1):
            return bool(number)
    raise ValueError('Artifact entry skip_cache must be boolean')


def fields(data):
    """Validate all field inputs before inferring size from a local file.

    Main callers: entry constructor. Missing/invalid values raise ValueError
    without Pydantic's structured error object; path converter TypeErrors and
    local-filesystem errors keep their native public exception types.
    Unknown model fields are ignored, matching pinned BaseModel configuration.
    """
    for name in ('path', 'digest'):
        if name not in data:
            raise ValueError(f'ArtifactManifestEntry requires {name}')
    path = LogicalPath(data['path'])
    digest = _string(data['digest'], 'digest')
    ref = _string(data.get('ref'), 'ref', optional=True)
    birth = _string(data.get('birthArtifactID', data.get('birth_artifact_id')), 'birth_artifact_id', optional=True)
    size = _size(data.get('size'))
    extra = data.get('extra', {})
    if not isinstance(extra, Mapping):
        raise ValueError('Artifact entry extra must be a dictionary')
    extra = {_string(key, 'extra key'): value for key, value in extra.items()}
    local = str(data['local_path']) if data.get('local_path') else None
    skip_cache = _boolean(data.get('skip_cache', False))
    if size is None and local:
        size = os.path.getsize(local)
    return dict(path=path, digest=digest, ref=ref, birth_artifact_id=birth, size=size,
                extra=extra, local_path=local, skip_cache=skip_cache,
                sha256=data.get('sha256'), _parent=data.get('_parent'), _reference=data.get('_reference'))



class _Factory:
    def __repr__(self):
        return '<factory>'


ENTRY_SIGNATURE = inspect.Signature([
    inspect.Parameter('path', inspect.Parameter.KEYWORD_ONLY, annotation=LogicalPath),
    inspect.Parameter('digest', inspect.Parameter.KEYWORD_ONLY, annotation=str),
    *[inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default)
      for name, default in [('ref',None),('birthArtifactID',None),('size',None),
                            ('extra',_Factory()),('local_path',None),('skip_cache',False)]]
], return_annotation=None)
