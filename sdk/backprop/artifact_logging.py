"""Publish fresh or existing artifact outputs through ordinary run journals.

Architecture: validate publication options -> prepare the artifact -> validate
the visible collection -> append immutable files and descriptive edits. A fresh
draft becomes one snapshot; incremental drafts first inherit a visible base. Distributed assembly
is still the separate upsert/finish path. Main callers: Run.log_artifact/model.
"""
import copy
import json
import uuid
import os
import re

from .artifacts import Artifact, _validate_metadata
from .artifact_updates import logging_aliases, validate_aliases


def publication_tags(tags):
    """Apply the pinned publication validator before preparing a path.

    Main caller: log_artifact. Validation order and Python errors are observable;
    the property setter has its own contract and is intentionally separate.
    """
    if tags is None:
        return None
    values = [tags] if isinstance(tags, (str, bytes)) else list(tags)
    values = list(dict.fromkeys(values))
    if any(re.match(r"^[-\w]+( +[-\w]+)*$", tag) is None for tag in values):
        raise ValueError("Invalid tag(s).  Tags must only contain alphanumeric characters "
                         "separated by hyphens, underscores, and/or spaces.")
    return values


def prepare(run, artifact_or_path, name, type):
    """Resolve genuine path inputs while keeping existing Artifact objects.

    Main caller: log_artifact. os.path.basename deliberately retains the pinned
    trailing-directory separator behavior. Object inputs ignore name/type.
    """
    artifact = artifact_or_path
    if isinstance(artifact_or_path, (str, os.PathLike)):
        artifact = Artifact(name or f"run-{run.id}-{os.path.basename(artifact_or_path)}", type or "unspecified")
        if os.path.isfile(artifact_or_path):
            artifact.add_file(str(artifact_or_path))
        elif os.path.isdir(artifact_or_path):
            artifact.add_dir(str(artifact_or_path))
        elif "://" in str(artifact_or_path):
            artifact.add_reference(str(artifact_or_path))
        else:
            raise ValueError("path must be a file, directory or externalreference like s3://bucket/path")
    if not isinstance(artifact, Artifact):
        raise TypeError("You must pass an instance of wandb.Artifact or a valid file path to log_artifact")
    artifact.finalize()
    return artifact


def validate_output(artifact, run):
    """Apply collection and ownership checks to every publication entry point.

    Main callers: log_artifact and distributed contribution. Offline calls defer
    service checks; committed cross-project/entity moves reject online.
    """
    if not run._settings._offline:
        from .artifact_publication import validate_collection_type
        validate_collection_type(artifact, run, preserve_owner=False)
        if artifact.state == 'COMMITTED' and artifact.entity and run.entity and artifact.entity != run.entity:
            raise ValueError(f"Artifact {artifact.name} is owned by entity '{artifact.entity}'; it can't be moved to '{run.entity}'")
        if artifact.state == 'COMMITTED' and artifact.project and run.project and artifact.project != run.project:
            raise ValueError(f"Artifact {artifact.name} exists in project '{artifact.project}'; it can't be moved to '{run.project}'")


def log_artifact(run, artifact_or_path, name=None, type=None, aliases=None, tags=None):
    """Queue a complete immutable output; logging itself does not await upload.

    Main callers: logging Run.log_artifact and Run.log_model. Aliases/tags reject
    before filesystem work. Incremental=True inherits the visible previous version before publication;
    explicit upsert/finish calls separately collect distributed contributions.
    """
    aliases = None if aliases is None else validate_aliases(aliases)
    tags = publication_tags(tags)
    artifact = prepare(run, artifact_or_path, name, type)
    aliases = logging_aliases(aliases)
    artifact.metadata = _validate_metadata({**artifact.metadata})
    artifact.distributed_id = None
    validate_output(artifact, run)
    if artifact._logged:
        return relog(run, artifact, aliases, tags)
    from .artifact_incremental_logging import inherit_visible_base
    inherit_visible_base(artifact, run)
    return artifact._log_to_run(run, aliases=aliases, tags=tags)


def relog(run, artifact, aliases, tags):
    """Add another output relationship without altering immutable content.

    Main caller: log_artifact for queued or committed objects. Copy only the
    original manifest's referenced files, then queue ordinary descriptive edits.
    The current run journal owns retry/restart; an offline producer need not sync.
    """
    from .artifact_events import iter_artifact_events
    from .config_artifacts import JournalArtifactStore
    from .journal_files import copy_references
    from .transport import read_journal
    owner = getattr(artifact, '_owner', None)
    source = getattr(owner, '_journal', None)
    directory = source.directory if source is not None else artifact._store.directory if isinstance(artifact._store, JournalArtifactStore) else None
    if (directory is not None and run._sink is not None
            and (artifact.entity, artifact.project) == (run.entity, run.project)
            and (getattr(artifact, "_incremental_pending", False) or getattr(artifact, "_distributed_pending", False))):
        from .deferred_incremental_artifacts import prepare_source
        prepare_source(artifact, directory, run._sink)
    events = read_journal(directory) if directory is not None else artifact._store.read_events(entity=artifact.entity, project=artifact.project)
    original = next((e for e in iter_artifact_events(events) if e['payload']['artifact_id']==artifact._id
                     and e.get('entity', '')==artifact.entity and e['project']==artifact.project), None)
    if original is None:
        raise RuntimeError('The existing artifact has no durable immutable manifest')
    payload = copy.deepcopy(original['payload'])
    if (artifact.entity, artifact.project) != (run.entity, run.project):
        # Alias ownership is scoped to the destination collection. The original
        # manifest's source aliases must not leak into a new namespace.
        # A copy is a distinct destination snapshot. Derive its ID from the
        # immutable source and destination so independent retries converge,
        # without reusing an ID whose incremental base can differ by project.
        identity = [artifact.entity, artifact.project, artifact._id, payload['version'],
                    run.entity, run.project]
        payload['artifact_id'] = uuid.uuid5(uuid.NAMESPACE_URL,
            'backprop:artifact-output-copy:' + json.dumps(identity, separators=(',', ':'))).hex
        # Every copy has the same immutable baseline; publication options live
        # in the following update event, including the first set of aliases.
        payload['aliases'] = []
    if directory is not None:
        payload['files'] = copy_references(payload.get('files', []), directory, run._journal)
    committed = artifact.state == 'COMMITTED'
    same_scope = (artifact.entity, artifact.project) == (run.entity, run.project)
    values = {'metadata': copy.deepcopy(artifact.metadata), 'description': artifact.description,
              'tags': list(artifact.tags if tags is None else tags)}
    with run._lock:
        run._emit('artifact', payload)
        run._emit('artifact_update', {'artifact_id':payload['artifact_id'], 'name':artifact._name,
            'values':values, 'alias_add':aliases, 'alias_remove':[]})
        if not committed:
            artifact._contribution_handle = None
            artifact._owner = run
            artifact._journal_count = run._journal.count
            artifact._store = run._sink
            # A pending handle follows its latest durable output publication.
            # Keep the scope and producer aligned with that journal so later
            # input handoff, refresh and restart validation select its envelope.
            artifact._entity, artifact._project, artifact._run_id = run.entity, run.project, run.id
            artifact._id = payload['artifact_id']
        if not committed or same_scope:
            # An offline copy into another namespace does not edit an already
            # committed source object or its saved mutation baseline.
            artifact._aliases = list(dict.fromkeys([*artifact._aliases, *aliases])) if same_scope else list(aliases)
            artifact._tags = values['tags']
            from .artifact_updates import enable_edits
            enable_edits(artifact)
        for ref in payload.get('files', []):
            if '_backprop_local_file' in ref:
                object.__setattr__(artifact._entries[ref['name']], 'local_path', str(run._journal.directory/ref['_backprop_local_file']))
    return artifact
