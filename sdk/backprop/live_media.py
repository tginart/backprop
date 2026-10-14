"""Run-media links that freeze once before their first network attempt.

Architecture: bind a caller path under run.dir -> journal a private binding ID ->
first flush copies linked bytes into immutable staging -> atomically persist the
chosen snapshot -> upload. Every retry and process restart reuses that snapshot.
Artifacts and table cells do not use this boundary: their content is immutable
when encoded. The original path may change until freezing, as allow_media_symlink
promises; after freezing neither source edits nor deletion can change the event.
Main callers: Journal.stage_live_media/freeze_live_media, Encoder, local forks.
"""
from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
import warnings

from .transport import BackpropError


def _under(root, relative, *, follow_file=True):
    path = root / relative
    checked = path.resolve() if follow_file else path.parent.resolve()
    if Path(relative).is_absolute() or not checked.is_relative_to(root.resolve()):
        raise BackpropError("Live media path escapes the run directory")
    return path


def _write(path, value, *, only_if_absent=False):
    """Commit binding state before allowing a network request to observe it."""
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(value, output, allow_nan=False, separators=(',', ':'))
            output.flush(); os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if only_if_absent:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            except OSError as error:
                raise BackpropError("The journal filesystem cannot atomically choose a linked-media snapshot") from error
        else:
            temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def bind(directory, source, *, symlink=True, temporary=False):
    """Retain a source inside the journal until its upload snapshot is chosen.

    Main callers: Journal.stage_live_media and prepare_live_media. Caller-owned
    paths can link so later file edits remain visible until upload. Generated
    temporary media must copy immediately: its process may exit before sync.
    This ownership distinction prevents dangling links without an extra service.
    """
    directory, source = Path(directory), Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    identity = uuid.uuid4().hex
    name = source.name
    relative = f'files/media/{identity}/{name}'
    destination = _under(directory, relative, follow_file=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    method = None
    if temporary:
        shutil.copy2(source, destination)
        method = 'copy'
    if method is None and symlink:
        try:
            destination.symlink_to(source)
            method = 'symlink'
        except (OSError, NotImplementedError):
            pass
    if method is None:
        try:
            os.link(source, destination)
            method = 'hardlink'
        except (OSError, NotImplementedError):
            pass
    if method is None:
        shutil.copy2(source, destination)
        method = 'copy'
        warnings.warn('Media could not be linked; copied bytes will not reflect later source edits', RuntimeWarning, stacklevel=3)
    state_dir = _under(directory, 'live-media')
    state_dir.mkdir(parents=True, exist_ok=True)
    _write(state_dir / f'{identity}.json', {'version': 1, 'id': identity, 'name': name,
           'link': relative, 'source': str(source), 'method': method})
    return {'_backprop_live_media': identity, 'name': name, 'mime_type': mimetypes.guess_type(name)[0]}


def freeze(journal, reference):
    """Read or atomically choose one immutable snapshot, without uploading it.

    Main callers: Journal._resolve and local fork copy. All private paths are
    confined before reading. A vanished source before freezing errors with the
    journal untouched; a vanished source afterward is irrelevant to recovery.
    """
    identity = reference.get('_backprop_live_media')
    name = reference.get('name')
    if not isinstance(identity, str) or re.fullmatch('[0-9a-f]{32}', identity) is None:
        raise BackpropError('Invalid live media binding ID')
    if not isinstance(name, str) or name in {'', '.', '..'} or Path(name).name != name:
        raise BackpropError('Invalid live media filename')
    state_path = _under(journal.directory, f'live-media/{identity}.json')
    if state_path.is_symlink():
        raise BackpropError('Live media binding metadata cannot be a symlink')
    state = json.loads(state_path.read_text(encoding='utf-8'))
    relative = f'files/media/{identity}/{name}'
    if (state.get('version') != 1 or state.get('id') != identity or state.get('name') != name
            or state.get('link') != relative or state.get('method') not in {'symlink', 'hardlink', 'copy'}):
        raise BackpropError('Live media binding metadata does not match its queued reference')
    choice = _under(journal.directory, f'live-media/{identity}.snapshot.json')
    if choice.is_symlink():
        raise BackpropError('Frozen live media metadata cannot be a symlink')
    if choice.exists():
        snapshot = json.loads(choice.read_text(encoding='utf-8'))
        digest = snapshot.get('sha256', '') if isinstance(snapshot, dict) else ''
        if (not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
                or snapshot.get('name') != name or snapshot.get('_backprop_local_file') != f'files/{digest}/{name}'):
            raise BackpropError('Invalid frozen live media snapshot')
        return snapshot
    linked = _under(journal.directory, relative, follow_file=False)
    if state['method'] == 'symlink':
        if not linked.is_symlink() or str(linked.readlink()) != state.get('source'):
            raise BackpropError('Live media link target differs from the authorized source')
    elif linked.is_symlink():
        raise BackpropError('Live media hardlink or copy was replaced with a symlink')
    snapshot = journal.stage_file(linked, name=name)
    # No replacement is allowed here: a parent uploader and a local fork can
    # resolve the same binding concurrently through different Journal objects.
    # Both must reload the winning record before returning uploadable bytes.
    _write(choice, snapshot, only_if_absent=True)
    return freeze(journal, reference)
