"""Capture completed, checksum-verified pinned W&B offline transaction logs.

Architecture: caller finishes its real run -> public W&B service teardown ->
one immutable transaction byte snapshot -> checksum-protected protobuf Records.
Main callers: TensorBoard parity probes and actual framework-init probes.

Why this exists: final core summaries arrive before the transaction writer
necessarily flushes. Every probe must use the same shutdown boundary and save
the exact bytes it decoded. This importable module has no probe execution or
SDK import side effects; W&B imports happen only when its functions are called.
The byte parser follows pinned transactionlog/leveldb source and never opens a
path, retries an observation or substitutes values from a final summary.
"""
from __future__ import annotations

from pathlib import Path
import struct
import zlib

__all__ = ["wandb_records", "completed_reference_records"]


def wandb_records(data):
    """Read the pinned Go core's actual checksum-protected transaction records.

    Main caller: completed_reference_records. The seven-byte W&B file header
    precedes LevelDB chunks. FIRST/MIDDLE/LAST fragments form one protobuf Record.
    The caller freezes bytes after teardown, so decoding and raw evidence use
    exactly the same complete transaction log. This routine receives bytes,
    rather than a file path, to keep capture separate from interpretation.
    """
    from wandb.proto.wandb_internal_pb2 import Record
    if data[:7] != b":W&B\xe1\xbe\x00":
        raise ValueError("Unexpected pinned W&B transaction log header")
    offset, parts = 7, bytearray()
    while offset < len(data):
        remaining = 32768 - offset % 32768
        if remaining < 7:
            offset += remaining
            continue
        crc, length, kind = struct.unpack_from("<IHB", data, offset)
        if (crc, length, kind) == (0, 0, 0):
            offset += remaining
            continue
        payload = data[offset + 7:offset + 7 + length]
        if len(payload) != length or length + 7 > remaining:
            raise ValueError("Truncated W&B transaction chunk")
        if zlib.crc32(bytes([kind]) + payload) != crc:
            raise ValueError("W&B transaction chunk checksum mismatch")
        if kind in (1, 2):
            parts = bytearray(payload)
        elif kind in (3, 4):
            parts.extend(payload)
        else:
            raise ValueError("Unknown W&B transaction chunk type")
        if kind in (1, 4):
            yield Record.FromString(bytes(parts))
            parts.clear()
        offset += 7 + length


def completed_reference_records(run):
    """Close the offline reference service and return its Records and bytes.

    Main callers: supported/unsupported TensorBoard writer probes, transaction
    readback, and actual framework-init probes after their run.finish calls.
    Why this exists: run.finish obtains final core summary/history responses
    before the independent transaction-log writer necessarily flushes its
    buffer. Public wandb.teardown waits for service shutdown; the core joins its
    writer goroutine and closes that log. This is one lifecycle boundary, with
    no observation retries, sleeps or replacement summary values.
    """
    import wandb
    if wandb.__version__ != "0.30.0":
        raise RuntimeError("Reference transaction capture requires pinned W&B0.30.0")
    wandb.teardown()
    transaction_bytes = Path(run._settings.sync_file).read_bytes()
    return list(wandb_records(transaction_bytes)), transaction_bytes
