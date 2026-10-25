"""Publish explicit evaluations independently of a training run's lifetime.

Architecture: bound EvalTable -> existing mutation journal -> ordinary artifact
manifest and immutable files -> existing Foundry transport. The original Run
remains disabled or finished; no run lifecycle event or history step is added.
Main caller: EvalTable.to_json. Ordinary active-run and Run.log publication still
share the training journal, preserving its atomic history/artifact transaction.
Why this exists: W&B's evaluation store accepts explicit publication even when
experiment logging is disabled or finished. A separate local journal preserves
that behavior without reopening a run, starting a worker or adding a service.
"""
from __future__ import annotations
from pathlib import Path
import uuid


def _writer_for(run):
    """Lazily create one existing mutation writer scoped to the bound run.

    Main caller: publish. A disabled run authenticates only for this explicit
    publication; ordinary disabled logging continues to do no persistence/I/O.
    Finished online runs reuse their authenticated store. Finished offline runs
    retain the existing local-queue extension and can later use ordinary sync.
    """
    writer = getattr(run, '_evaluation_writer', None)
    if writer is not None:
        return writer
    from .apis.public.mutations import MutationWriter
    store = run._sink
    if run.disabled and store is None:
        from backprop_backend.connection import authenticate_candidate, persist_candidate
        from .transport import native_store
        from .errors import UsageError
        candidate = authenticate_candidate(key=run._settings.api_key,
            host=run._deployment.get('host'), prompt=True, verify=False,
            timeout=None, settings=run._settings, load_config=lambda: run._deployment,
            native_store=native_store, config=run._deployment)
        if candidate.mode != 'online':
            raise UsageError('Explicit evaluation publication requires Foundry credentials')
        if candidate.source == 'prompt':
            persist_candidate(candidate, run._settings)
        store = native_store(run._deployment, token=candidate.token)
    writer = MutationWriter(store, run.entity, run.project, run.id,
        directory=Path(run.sync_dir) / 'evaluations' / uuid.uuid4().hex)
    # prepare_table needs these small bits of context, not a live training Run.
    writer._settings, writer.summary = run._settings, run.summary
    writer.resumed, writer.step = False, run.step
    writer._last_time = max(writer._last_time, run._last_event_time)
    run._evaluation_writer = writer
    return writer


def publish(table, run):
    """Append one evaluation before committing its identity, then upload it.

    Main caller: EvalTable.to_json with the original run lock held. An append
    failure leaves the value unpublished. An upload failure keeps the committed
    snapshot and exact event ID; repeated to_json or sync retries those bytes.
    Files and the manifest are recoverable after the process exits.
    """
    from .data_types import Encoder
    from .implicit_artifacts import prepare_table
    writer = _writer_for(run)
    with writer._lock:
        writer.flush()
        encoder = Encoder(writer._journal.stage_file, settings=run._settings,
            run=run, artifact_factory=lambda key, value, descriptor:
                prepare_table(writer, key, value, descriptor))
        encoder.key = table._run_log_key
        encoder.encode(table)
        if len(encoder.artifacts) != 1:
            raise RuntimeError('An evaluation must publish exactly one artifact manifest')
        manifest = encoder.artifacts[0]
        if run.disabled:
            manifest = {**manifest, 'logging_disabled': True}
        writer._emit('artifact', manifest)
        encoder.commit()
        table._evaluation_publisher = writer
        writer.flush()
        return dict(table._immutable_logged_json)


def flush_published(table):
    """Retry a previously committed explicit publication before returning its ID.

    Main caller: EvalTable.to_json's immutable cached-result branch. This is
    separate from initial publication so no retry re-encodes mutated raw cells.
    """
    writer = getattr(table, '_evaluation_publisher', None)
    if writer is not None:
        with writer._lock:
            writer.flush()
