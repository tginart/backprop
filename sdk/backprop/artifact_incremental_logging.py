"""Freeze the visible base of an incremental artifact before publication.

Architecture: read visible remote and current-journal manifests -> choose the
latest collection version -> inherit untouched entries -> overlay the new files
-> freeze that complete manifest in the ordinary output journal. Main caller:
artifact_logging.log_artifact. Retry never chooses a different base for an
already prepared object. Offline deltas defer base selection until sync or an
online artifact handoff, using a durable materialization cache.
"""
from dataclasses import replace
from datetime import datetime

from .artifact_events import iter_artifact_events
from .artifacts import _thaw
from .event_order import ordered_events
from .transport import read_journal


def inherit_visible_base(artifact, run):
    """Copy unchanged manifest entries without downloading remote file contents.

    Main caller: log_artifact for a fresh incremental draft. New entries win at
    duplicate paths. Only files are inherited; the new metadata and description
    belong to the new version. A prepared base survives an append retry.
    """
    if not artifact.incremental or artifact._logged or getattr(artifact, '_incremental_prepared', False):
        return
    if run._sink is None:
        artifact._incremental_pending=True
        return
    local=read_journal(run.sync_dir,sink=run._sink)
    remote=run._sink.read_events(entity=run.entity,project=run.project) if run._sink else []
    # A journal and stream can carry the same envelope with resolved file paths.
    # Prefer the local snapshot for those identities before canonical replay.
    visible={event['event_id']:event for event in [*remote,*local]}
    candidates=[e for e in iter_artifact_events(ordered_events(visible.values()))
                if e['payload']['name']==artifact._name and e['payload']['artifact_type']==artifact.type]
    if candidates:
        # Re-logging an old output must not make it the newest created version.
        first={}
        for candidate in candidates:first.setdefault(candidate['payload']['artifact_id'],candidate)
        event=max(first.values(),key=lambda e:(datetime.fromisoformat(e['payload'].get('created_at',e['timestamp']).replace('Z','+00:00')),e['payload']['artifact_id']))
        from .artifacts import Artifact
        base=Artifact._from_manifest(event['payload'],run._sink,run.entity,run.project,event['run_id'])
        inherited={}
        for name,entry in base._entries.items():
            if name in artifact._entries:continue
            ref=_thaw(entry._reference)
            local_path=str(run._journal.directory/ref['_backprop_local_file']) if '_backprop_local_file' in ref else None
            inherited[name]=replace(entry,_parent=artifact,local_path=local_path,_reference=ref)
        artifact._entries={**inherited,**artifact._entries}
        artifact._incremental_base_id=base._id
    artifact._incremental_prepared=True
