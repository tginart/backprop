"""Pinned W&B0.30.0 tracking_handler algorithms, with local imports only.

Architecture: provider metadata creates immutable reference entries; download
resolves provider versions and applies the original integrity checks. Local
runtime supplies optional imports, HTTP/cache adapters and native entry values.
Main callers: external_references handler dispatch.
Upstream MIT source SHA256: 36d6f693875c567b5ae3b0c0afe0b356e5e2863678612f299744a5a1b687bd27
See THIRD_PARTY_NOTICES.md. Function/class bodies are retained verbatim.
"""
from __future__ import annotations

from urllib.parse import urlparse
from .runtime import termwarn, ArtifactManifestEntry, StorageHandler, FilePathStr

class TrackingHandler(StorageHandler):
    _scheme: str

    def __init__(self, scheme: str = "") -> None:
        """Track paths with no modification or special processing.

        Useful when paths being tracked are on file systems mounted at a standardized
        location.

        For example, if the data to track is located on an NFS share mounted on
        `/data`, then it is sufficient to just track the paths.
        """
        self._scheme = scheme

    def can_handle(self, parsed_url: ParseResult) -> bool:
        return parsed_url.scheme == self._scheme

    def load_path(
        self,
        manifest_entry: ArtifactManifestEntry,
        local: bool = False,
    ) -> URIStr | FilePathStr:
        if local:
            # Likely a user error. The tracking handler is
            # oblivious to the underlying paths, so it has
            # no way of actually loading it.
            url = urlparse(manifest_entry.ref)
            raise ValueError(
                f"Cannot download file at path {str(manifest_entry.ref)}, scheme {str(url.scheme)} not recognized"
            )
        # TODO(spencerpearson): should this go through util.to_native_slash_path
        # instead of just getting typecast?
        return FilePathStr(manifest_entry.path)

    def store_path(
        self,
        artifact: Artifact,
        path: URIStr | FilePathStr,
        name: StrPath | None = None,
        checksum: bool = True,
        max_objects: int | None = None,
    ) -> list[ArtifactManifestEntry]:
        url = urlparse(path)
        if name is None:
            raise ValueError(
                f'You must pass name="<entry_name>" when tracking references with unknown schemes. ref: {path}'
            )
        termwarn(
            f"Artifact references with unsupported schemes cannot be checksummed: {path}"
        )
        name = name or url.path[1:]  # strip leading slash
        return [ArtifactManifestEntry(path=name, ref=path, digest=path)]

