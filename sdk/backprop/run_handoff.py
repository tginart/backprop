"""Transfer logger ownership between processes without a background service.

Architecture: existing Run -> serializable identity/settings/config descriptor
-> independent secondary Run and writer journal. The original process remains
primary. A durable marker enables a resumed primary to keep tagging its events,
so later parent logs cannot replace another writer's summary contributions.
Main callers: Lightning Fabric logger pickling and experiment restoration.
Credentials supplied explicitly remain process-transfer data, never events.
"""
from __future__ import annotations

import copy
from pathlib import Path

from .artifacts import Artifact
from .artifact_events import iter_artifact_events
from backprop_backend.connection import snapshot_store, restore_store
from .transport import read_journal


def snapshot_run(run):
    """Capture detached inputs and mark subsequent parent events as shared.

    Main callers: WandbLogger.__getstate__. Never pickle locks, upload threads,
    open HTTP handlers or the live Run object. A supplied backend transport
    is reconstructed from its connection inputs in the receiving process.
    """
    with run._lock:
        run._check()
        if not run.disabled:
            before = run._shared
            run._shared = True
            try:
                run._emit('run_metadata', {'values': {}, '_handoff_parent': True})
            except BaseException:
                run._shared = before
                raise
        settings = {key: copy.deepcopy(run._settings._values[key])
                    for key in run._settings._explicit}
        for key in ['resume', 'resume_from', 'fork_from', 'run_id', 'run_name',
                    'entity', 'project', 'root_dir', 'mode']:
            settings.pop(key, None)
        settings.update(x_primary=False, x_update_finish_state=False, x_label=None)
        values = {key: value._reference_json() if isinstance(value, Artifact) else copy.deepcopy(value)
                  for key, value in run.config._data.items()}
        ids = {value['artifact_id'] for value in values.values()
               if isinstance(value, dict) and value.get('_type')=='artifact'}
        manifests = [event for event in iter_artifact_events(read_journal(run.sync_dir, sink=run._sink))
                     if event['payload'].get('artifact_id') in ids] if ids else []
        transport = snapshot_store(run._sink)
        return {'version':1, 'id':run.id, 'entity':run.entity, 'project':run.project,
                'name':run.name, 'mode':run.mode, 'dir':str(Path(run._settings._wandb_dir).parent),
                'settings':settings, 'deployment':dict(run._deployment), 'transport':transport,
                'config':values, 'config_manifests':manifests, 'source_dir':run.sync_dir,
                'definitions':copy.deepcopy(run._definitions), 'timestamp':run._last_event_time.isoformat()}


def restore_run(descriptor):
    """Open a secondary writer, preserving unrelated process-global runs.

    Main callers: a deserialized WandbLogger.experiment. Reusing the exact live
    experiment in the same process avoids creating an unnecessary writer. A
    fresh process receives independent steps, configuration deltas and cleanup.
    """
    import backprop
    if descriptor.get('version') != 1:
        raise ValueError('Unsupported Backprop logger transfer version')
    scope = tuple(descriptor[key] for key in ['entity','project','id'])
    active = backprop.run
    if active is not None and not active._closed and (active.entity,active.project,active.id)==scope:
        return active
    transport = descriptor['transport']
    sink = restore_store(transport)
    run = backprop.init(**{key:descriptor[key] for key in ['id','entity','project','name','mode','dir']},
        settings=descriptor['settings'], _sink=sink, _deployment=descriptor['deployment'],
        _shared_writer=True, _event_time_floor=descriptor['timestamp'], reinit='create_new')
    try:
        run.config._data.update(copy.deepcopy(descriptor['config']))
        from .config_artifacts import hydrate_config
        object.__setattr__(run.config, '_data', hydrate_config(run, descriptor['config_manifests'], descriptor['source_dir']))
        run._inherit_config_artifacts(descriptor['config_manifests'], descriptor['source_dir'])
        for definition in descriptor['definitions'].values():
            # Restore only the declared metric options, not a prior writer tag.
            options = {key:value for key,value in definition.items() if key != '_writer'}
            name = options.pop('name')
            run.define_metric(name, **options)
    except BaseException:
        run.finish(exit_code=1)
        raise
    if active is None or active._closed:
        backprop.run = run
    return run


def restore_parent_identity(run, local):
    """Recover a primary's promoted writer identity from its own journal.

    Main callers: Run initialization after reading previous local records. A
    remote secondary cannot claim primary ownership through this local check.
    Returns whether this is a resumed parent, whose own prior steps are retained.
    """
    markers = [e for e in local if e['payload'].get('_handoff_parent') and e['payload'].get('_writer')]
    if not markers:
        return False
    writer = markers[-1]['payload']['_writer']
    run._shared = True
    run._writer_id = writer['id']
    run._writer_sequence = 1 + max(e['payload']['_writer']['sequence'] for e in local
        if e['payload'].get('_writer', {}).get('id')==writer['id'])
    return True
