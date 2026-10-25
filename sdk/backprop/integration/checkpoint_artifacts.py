"""Make framework checkpoint retries use the run's existing durable journal.

Architecture: candidate immutable checkpoint -> prior journal manifests -> new
artifact or alias-update event. The manifest is the recovery record, so a lost
acknowledgement or a new logger instance cannot create another artifact for the
same checkpoint contents/metadata. Changed checkpoints remain new versions.
Main callers: Lightning Fabric checkpoint callbacks. No remote read, additional
cache file or persistent service is needed to recognize a committed local write.
"""
from __future__ import annotations

from ..artifact_events import iter_artifact_events
from ..artifact_updates import logging_aliases
from ..transport import read_journal


def log_checkpoint(run, artifact, aliases):
    """Publish one checkpoint, or reapply aliases to its already journaled ID.

    Main callers: WandbLogger._scan_and_log_pytorch_checkpoints. A callback's
    in-memory mtime map skips unchanged files during normal training; this check
    handles the separate retry/restart stage when that map was never updated.
    Existing file bytes, metadata and the content version stay immutable.
    """
    aliases = logging_aliases(aliases)
    if not run.disabled:
        digest = artifact._content_digest([])
        with run._lock:
            for event in iter_artifact_events(read_journal(run.sync_dir, sink=run._sink)):
                prior = event['payload']
                if (prior['name'], prior['artifact_type'], prior['digest']) == (artifact.name, artifact.type, digest):
                    run._check()
                    run._emit('artifact_update', {'artifact_id': prior['artifact_id'], 'name': prior['name'],
                        'values': {}, 'alias_add': aliases, 'alias_remove': []})
                    return
            run.log_model(artifact, aliases=aliases)
    else:
        run.log_model(artifact, aliases=aliases)
