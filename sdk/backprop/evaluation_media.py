"""Adapt evaluation media without introducing a separate media store.

Architecture: validate local media -> preserve native files and display metadata
-> existing table/artifact codecs. Images retain their PIL-backed representation;
audio mirrors the pinned Weave 0.52.41 file boundary; video uses the established
MoviePy reader and closes it immediately. Nothing uploads before validation.
Main caller: evaluation.normalize. Unlike W&B's Weave objects, the returned
values remain Backprop media, retaining captions and native Foundry file paths.
The audio checks follow weave/type_handlers/Audio/audio.py; see notices.
"""
from __future__ import annotations
import base64
import binascii
import copy
from pathlib import Path
from .media import Audio, Image, Video


def local_value(value, column):
    """Validate a supported local value and return its publication snapshot.

    Main caller: evaluation.normalize after external-reference and nesting checks.
    Missing paths and malformed media are errors even in stub mode: that mode
    substitutes unsupported value kinds, not broken instances of supported media.
    """
    if isinstance(value, Image):
        value.image  # Match the reference's lazy image read before publication.
        return value
    path = getattr(value, '_path', None)
    if path is None:
        raise TypeError(f'Cannot convert {type(value).__name__} in column {column!r}: media object does not have a local file path')
    path = Path(path)
    if isinstance(value, Audio):
        if not path.exists() or path.suffix.lower() not in {'.wav', '.mp3'}:
            raise TypeError(f'Cannot convert Audio in column {column!r}: expected an existing WAV or MP3 file')
        data = path.read_bytes()
        if not data:
            raise TypeError(f'Cannot convert Audio in column {column!r}: audio data cannot be empty')
        try:
            decoded = base64.b64decode(data, validate=True)
        except binascii.Error:
            decoded = data
        if decoded == data:
            return value
        # Weave's file adapter decodes valid base64. Snapshot the transformed
        # bytes without changing the user's Audio or its construction-time hash.
        from .media_files import freeze_file
        result = copy.copy(value)
        result._set_file(freeze_file(decoded, path.suffix), is_tmp=True)
        result.data = decoded
        result.metadata = dict(value.metadata)
        return result
    if isinstance(value, Video):
        try:
            from moviepy.video.io.VideoFileClip import VideoFileClip
        except ImportError as error:
            raise ImportError('Evaluation video validation requires Backprop video dependencies') from error
        clip = VideoFileClip(str(path))
        clip.close()
    return value
