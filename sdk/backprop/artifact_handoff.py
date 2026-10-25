"""Publish one offline artifact without syncing its producer's other records.

Architecture: select the immutable source envelope -> copy only its files into
an isolated journal -> upload with original event identity -> acknowledge ->
rebind live entry handles. Main caller: Run.use_artifact and use_model.
The source journal, its acknowledgement cursor and artifact provenance stay
unchanged. Later full source sync delivers the same artifact event again.
"""
import copy
import json
import uuid

from .artifact_events import iter_artifact_events
from .config_artifacts import JournalArtifactStore
from .errors import UsageError
from .journal_files import copy_references
from .process_lock import exclusive_lock
from .transport import Journal, read_journal


def _source(artifact):
    """Locate an explicit offline source, including a restored config object.

    Main caller: publish_pending. An ordinary remote Artifact does not require
    a transfer; an unavailable local source is an error rather than empty data.
    """
    owner = getattr(artifact, '_owner', None)
    journal = getattr(owner, '_journal', None)
    if journal is not None and journal.sink is None:
        return journal.directory, lambda: read_journal(journal.directory)
    if isinstance(artifact._store, JournalArtifactStore):
        from .deferred_incremental_artifacts import cached_events
        namespace = getattr(artifact._store.remote, 'ack_namespace', None)
        return artifact._store.directory, lambda: cached_events(artifact._store.directory, artifact._store.events, namespace=namespace)
    return None


def publish_pending(artifact, run):
    """Transfer the selected original manifest with durable, repeatable writes.

    Main callers: use_artifact's pending-object and restored-local-object paths.
    The journal directory is stable for the consumer and immutable source identity. A process
    restart can replay it with ordinary sync even after the producer disappears.
    File copies and validation complete before the manifest becomes publishable.
    """
    if getattr(artifact, '_handoff_store', None) is run._sink:
        return
    source = _source(artifact)
    if source is None:
        return
    if run._sink is None or run._journal is None:
        raise UsageError('Publishing an offline artifact requires an online run')
    source_dir, source_events = source
    if (getattr(artifact, "_incremental_pending", False) or getattr(artifact, "_distributed_pending", False)):
        from .deferred_incremental_artifacts import prepare_source
        prepare_source(artifact, source_dir, run._sink)
        source_events = lambda: read_journal(source_dir, sink=run._sink)
    key = json.dumps([artifact.entity, artifact.project, artifact._run_id, artifact._id, artifact.version])
    identity = uuid.uuid5(uuid.NAMESPACE_URL, 'backprop:artifact-handoff:' + key).hex
    directory = run._journal.directory / 'artifact-inputs' / identity
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'publish.lock').open('a+b') as lock, exclusive_lock(lock):
        journal = Journal(directory, run._sink)
        previous = read_journal(directory, sink=run._sink)
        if previous:
            if len(previous) != 1:
                raise UsageError('Artifact transfer journal must contain exactly one immutable manifest')
            prepared = previous[0]
            payload = prepared.get('payload', {})
            if (prepared.get('type') != 'artifact' or prepared.get('entity', '') != artifact.entity
                    or prepared.get('project') != artifact.project or prepared.get('run_id') != artifact._run_id
                    or payload.get('artifact_id') != artifact._id or payload.get('version') != artifact.version
                    or payload.get('digest') != artifact.digest):
                raise UsageError('Artifact transfer journal conflicts with its immutable source')
        else:
            candidates = [event for event in iter_artifact_events(source_events())
                          if event['payload'].get('artifact_id') == artifact._id
                          and event.get('entity', '') == artifact.entity
                          and event['project'] == artifact.project]
            if not candidates:
                raise UsageError('The offline artifact has no durable source manifest')
            prepared = copy.deepcopy(candidates[0])
            prepared['payload']['files'] = copy_references(prepared['payload'].get('files', []), source_dir, journal)
            journal.append(prepared)
        artifact._handoff_journal = str(directory)
        journal.flush()
        published = journal._resolve(prepared)
    # Preserve existing Python entry handles while changing only their verified
    # storage location. Contents, digest, birth identity and ownership do not move.
    from .artifacts import _freeze
    for reference in published['payload'].get('files', []):
        entry = artifact._entries[reference['name']]
        object.__setattr__(entry, 'local_path', None)
        object.__setattr__(entry, '_reference', _freeze(reference))
    artifact._store = run._sink
    artifact._committed = True
    artifact._handoff_store = run._sink
    artifact._handoff_journal = str(directory)
