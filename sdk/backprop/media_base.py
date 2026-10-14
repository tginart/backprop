"""Public file-backed value contracts, separate from native media encoders.

Architecture: caption-only construction -> an extension sets its encoded file ->
explicit binding copies/moves/links it into one run -> the existing file journal
publishes that path. Artifacts use their existing manifests. No extra storage or
worker is needed. Native image/audio constructors live in media.EncodedMedia.
Main callers: Media extensions, BatchableMedia and explicit bind_to_run calls.
The portable file lifecycle follows pinned W&B 0.30.0 (THIRD_PARTY_NOTICES.md).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import re
import shutil

from .values import WBValue


def _filename(key, step, identity, extension):
    """Keep nested metric names while removing absolute and traversal paths.

    Main callers: Media.bind_to_run. This matches the upstream filename rule;
    the destination remains inside the run even for keys such as /../loss.
    """
    key = str(key)
    if platform.system() == "Windows":
        if re.search(r'[\\:*?"<>|]', key):
            raise ValueError(f"Path {key} is invalid. Please remove invalid filename characters")
        key = key.replace("/", os.sep)
    key = os.sep.join(part.replace(".", "_") if part in (".", "..") else part
                      for part in key.lstrip(os.sep).split(os.sep))
    return f"{key}_{step}_{identity}{extension}"


def _link_or_copy(source, destination, *, symlink):
    """Match the reference link order, atomically replacing the copy fallback.

    Main callers: explicit binding. Rebinding an existing destination must not
    unlink its source through a self-referential symlink.
    """
    if symlink:
        try:
            destination.symlink_to(source)
            return
        except (OSError, NotImplementedError):
            pass
    try:
        os.link(source, destination)
        return
    except (OSError, NotImplementedError):
        pass
    temporary = destination.with_name(destination.name + ".tmp~backprop")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class Media(WBValue):
    """Caption-only base for extensions that serialize an encoded file."""

    kind = "file"

    def __init__(self, caption=None):
        super().__init__()
        self._path = self._run = None
        self._caption = caption
        self.data, self.metadata = None, {}

    @property
    def caption(self):
        return self._caption

    @caption.setter
    def caption(self, value):
        self._caption = value

    @property
    def path(self):
        return Path(self._path) if self._path is not None and not self.path_is_reference(self._path) else None

    @path.setter
    def path(self, value):
        self._path = None if value is None else str(value)

    def _set_file(self, path, is_tmp=False, extension=None):
        self._path, self._is_tmp, self._extension = str(path), is_tmp, extension
        assert extension is None or self._path.endswith(extension), (
            f'Media file extension "{extension}" must occur at the end of path "{path}".')
        self._sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self._size = Path(path).stat().st_size

    @classmethod
    def get_media_subdir(cls):
        raise NotImplementedError

    @staticmethod
    def captions(media_items):
        return [item._caption for item in media_items] if media_items[0]._caption is not None else False

    @staticmethod
    def path_is_reference(path):
        if path is None or isinstance(path, Path):
            return False
        return bool(path and re.match(r"^(gs|s3|https?)://", path))

    def file_is_set(self):
        return self._path is not None and self._sha256 is not None

    def is_bound(self):
        return self._run is not None

    def bind_to_run(self, run, key, step, id_=None, ignore_copy_err=None):
        """Apply all naming and copy options, then publish via the run journal.

        Main callers: explicit media binding and user-defined file value types.
        Temporary files move; ordinary files copy unless linking was requested.
        SameFileError is suppressed only by the declared ignore_copy_err switch.
        """
        assert self.file_is_set(), "bind_to_run called before _set_file"
        assert run is not None, "Argument passed to bind_to_run must be a run object"
        self._run = self._bound_run = run
        extension = os.path.splitext(os.path.basename(self._path))[1] if self._extension is None else self._extension
        identity = self._sha256[:20] if id_ is None else id_
        relative = os.path.join(self.get_media_subdir(), _filename(key, step, identity, extension))
        destination = os.path.join(run.dir, relative)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        if self._is_tmp:
            shutil.move(self._path, destination)
            self._is_tmp = False
        elif run._settings.allow_media_symlink:
            _link_or_copy(Path(self._path).resolve(), Path(destination), symlink=run._settings.symlink)
        else:
            try:
                shutil.copy(self._path, destination)
            except shutil.SameFileError:
                if not ignore_copy_err:
                    raise
        self._path = destination
        run._publish_file(relative)

    def to_json(self, run):
        """Return portable file fields using the actual owning run or artifact.

        Main callers: extension serializers, which add their own _type fields.
        Native media encoders add Foundry file descriptors in EncodedMedia.
        """
        from .run import Run
        from .artifacts import Artifact
        from .media import Audio, Image
        result = {} if self._caption is None else {"caption": self._caption}
        if isinstance(run, Run):
            result.update(_type="file", sha256=self._sha256, size=self._size)
            reference = self._get_artifact_entry_ref_url()
            if reference is not None:
                result["artifact_path"] = reference
            latest_reference = self._get_artifact_entry_latest_ref_url()
            if latest_reference is not None:
                result["_latest_artifact_path"] = latest_reference
            if reference is None or self.is_bound():
                assert self.is_bound(), (
                    f"Value of type {type(self).__name__} must be bound to a run with bind_to_run() before being serialized to JSON.")
                assert self._run is run, "We don't support referring to media files across runs."
                result["path"] = os.path.relpath(self._path, run.dir)
        elif isinstance(run, Artifact):
            if self.file_is_set():
                name = run.get_added_local_path_name(self._path)
                if name is None:
                    pieces = [self.get_media_subdir()]
                    if not self._is_tmp:
                        pieces.append(self._sha256[:20])
                    pieces.append(os.path.basename(self._path))
                    name = "/".join(pieces)
                    if self._artifact_source is not None:
                        name = self._reference_source_file(run, name)
                    elif isinstance(self, (Audio, Image)) and Media.path_is_reference(self._path):
                        run.add_reference(self._path, name=name)
                    else:
                        name = run.add_file(self._path, name=name, is_tmp=self._is_tmp).path
                result.update(path=name, sha256=self._sha256)
            result["_type"] = self._log_type
        return result

    def _reference_source_file(self, artifact, name):
        """Reuse the source manifest entry, preserving its content and origin.

        Main callers: Media.to_json when an extension serializes a value read
        from another artifact. Downloaded paths follow the reference's default
        root rule. Native decoded values retain their exact content entry so
        temporary decoding files cannot be mistaken for new artifact contents.
        Why this exists: provenance must survive re-logging; equal bytes alone
        do not establish a relationship between two artifact versions.
        """
        source = self._artifact_source
        content_path = getattr(self, "_artifact_content_path", None)
        if content_path is not None:
            name = content_path
        else:
            default_root = source._default_root()
            if self._path.startswith(default_root):
                name = self._path[len(default_root):].lstrip(os.sep)
        entry = source.get_entry(name)
        entry.ref_url()  # Require a committed source identity before reuse.
        artifact.add_reference(entry, name=name)
        return name

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls(source_artifact.get_entry(json_obj["path"]).download())

    def __eq__(self, other):
        return (isinstance(other, self.__class__) and hasattr(self, "_sha256")
                and hasattr(other, "_sha256") and self._sha256 == other._sha256)


class BatchableMedia(Media):
    """Base for media whose concrete type supplies a batch descriptor."""

    def __init__(self, caption=None):
        super().__init__(caption=caption)

    @classmethod
    def seq_to_json(cls, seq, run, key, step):
        raise NotImplementedError
