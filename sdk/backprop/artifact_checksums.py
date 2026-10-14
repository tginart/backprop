"""Artifact file checksums with an optional xxHash implementation.

Architecture: select the declared entry algorithm -> stream immutable bytes
through that hasher and SHA-256 -> return W&B's base64 entry checksum. Manifest
checksums use W&B's fixed header and sorted path/checksum lines. Native artifact
version identities remain the separate SHA-256 metadata/content hash.
Main callers: Artifact creation, manifest entries, download caches and verify.
Why this exists: an XXH128 entry must never be verified with an MD5 transport
checksum, and requesting the optional algorithm must fail explicitly if absent.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path


def hasher(algorithm="MD5", data=b""):
    """Construct the requested hasher without importing optional code for MD5."""
    if algorithm in {"XXH128", "MANIFEST_XXH128"}:
        try:
            import xxhash
        except ImportError as error:
            raise ImportError("XXH128 artifact checksums require the optional xxhash package; install backprop-foundry[checksums]") from error
        return xxhash.xxh128(data)
    if algorithm in {"MD5", "MANIFEST_MD5"}:
        return hashlib.md5(data, usedforsecurity=False)
    raise ValueError(f"Unknown artifact checksum algorithm: {algorithm!r}")


def bytes_digest(data, algorithm="MD5"):
    return base64.b64encode(hasher(algorithm, data).digest()).decode("ascii")


def file_checksums(path, algorithm="MD5"):
    """Hash exactly one immutable snapshot, independently retaining SHA-256.

    Main callers: Artifact.add_file, entry cache lookup and Artifact.verify.
    Both hashes observe the same byte stream, including empty files.
    """
    selected, integrity, size = hasher(algorithm), hashlib.sha256(), 0
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            selected.update(chunk)
            integrity.update(chunk)
            size += len(chunk)
    return base64.b64encode(selected.digest()).decode("ascii"), integrity.hexdigest(), size


def manifest_digest(entries, algorithm="MD5"):
    """Match the pinned v1 manifest checksum without changing native versions."""
    selected = hasher(algorithm, b"wandb-artifact-manifest-v1\n")
    for path, entry in sorted(entries.items()):
        selected.update(f"{path}:{entry.digest}\n".encode())
    return selected.hexdigest()
