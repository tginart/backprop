"""Copy queued file references without changing their immutable contents.

Architecture: inspect local references -> confine reads to the source journal
-> stage an independent snapshot -> verify the expected hash and size -> retain
all other reference metadata. Main callers: run forks and artifact handoffs.
"""
from pathlib import Path
from .errors import UsageError


def copy_references(value, source_dir, destination):
    """Copy only files reachable from the supplied value into another journal.

    Main callers: Run._copy_branch_files and artifact_handoff.publish_pending.
    Staging streams bytes; large artifacts do not require one in-memory copy.
    No containing event is appended until every referenced snapshot validates.
    """
    if isinstance(value, dict):
        if '_backprop_live_media' in value:
            from .transport import Journal
            snapshot = Journal(source_dir).freeze_live_media(value)
            visible = {key: item for key, item in value.items() if key != '_backprop_live_media'}
            return copy_references({**snapshot, **visible}, source_dir, destination)
        if '_backprop_local_file' in value:
            source = Path(source_dir).resolve()
            path = (source / value['_backprop_local_file']).resolve()
            if not path.is_relative_to(source):
                raise UsageError('Queued file reference escapes its source directory')
            copied = destination.stage_file(path, name=value.get('name'))
            if copied['sha256'] != value.get('sha256') or copied['size'] != value.get('size'):
                raise UsageError('Queued file failed immutable SHA-256/size validation during transfer')
            return {key: copy_references(item, source_dir, destination)
                    for key, item in {**value, **copied}.items()}
        return {key: copy_references(item, source_dir, destination) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_references(item, source_dir, destination) for item in value]
    return value
