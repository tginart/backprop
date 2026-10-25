"""Recover unfinished distributed operations from their durable writer journals.

Architecture: content fingerprint -> unacknowledged matching intent, or fresh
operation ID -> part/final events -> normal sync. A completed operation does not
reserve its fingerprint forever; another call can publish identical content in
the same group. Main callers: distributed artifact contribution/finalization.
"""
import hashlib
import json
from pathlib import Path

from .transport import Journal, read_journal


def fingerprint(run, group, manifest):
    """Identify retry intent independently of new draft UUIDs and timestamps.

    Main callers: contribution. An explicit shared x_label identifies a logical
    writer across restarts. Unlabeled handles use their generated writer ID;
    identical bytes never authorize recovering another writer's journal. The
    list representation distinguishes generated IDs from caller-owned labels.
    """
    value = {key: manifest.get(key) for key in ('name', 'artifact_type', 'digest', 'metadata', 'description', 'aliases', 'tags')}
    value.update(entity=run.entity, project=run.project, run_id=run.id,
                 group=group, writer=(run._settings.x_label or ['writer', run._writer_id]) if run._shared else None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def operation_id(run, group, manifest):
    """Reuse only a durable unacknowledged intent; otherwise start a new call.

    Main caller: contribution. Restarted shared handles inspect journals of the
    same logical writer through the fingerprint. Successfully completed rounds
    never suppress a later identical contribution under the same distributed ID.
    """
    import uuid
    from .deferred_branch import read_local_branch
    value=fingerprint(run,group,manifest)
    for path in writer_journals(run):
        events=read_journal(path,project_artifacts=False)
        journal=Journal(path,_branch_prepared=read_local_branch(path) is not None)
        acknowledged=journal.acknowledged_count()
        for position,event in enumerate(events,1):
            payload=event['payload']
            if (position>acknowledged and event['type'] in {'artifact_part','artifact'}
                    and payload.get('operation_fingerprint',payload.get('operation_id'))==value):
                return payload['operation_id']
    return uuid.uuid4().hex


def writer_journals(run):
    """Find local retry evidence without reading or synchronizing other writers.

    Main callers: identity selection and recovery. Only explicitly labeled
    handles can find prior logical-writer journals. An unlabeled handle owns its
    current directory; a previous unlabeled process is recovered with sync(path).
    """
    current=Path(run.sync_dir)
    return [current] if not run._shared or not run._settings.x_label else sorted(path.parent for path in current.parent.glob('*/events.jsonl'))


def recover_local(run, identity, kind):
    """Replay an existing operation's journal before returning its envelope.

    Main callers: contribution/finish. Only journals containing this exact
    operation are retried; unrelated shared writers are never synchronized here.
    Concurrent use of the same logical writer identity is not supported.
    """
    current = Path(run.sync_dir)
    for path in writer_journals(run):
        matching = [event for event in read_journal(path,project_artifacts=False)
                    if event['type'] == kind and event['payload'].get('operation_id') == identity]
        if matching:
            journal = run._journal if path == current else Journal(path, run._sink)
            journal.flush()
            recovered=next(event for event in read_journal(path,sink=run._sink) if event['event_id']==matching[0]['event_id'])
            return journal._resolve(recovered) if run._sink is not None else recovered
    return None
