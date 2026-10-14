"""Record artifact inputs after the required publication stage completes.

Architecture: validate lifecycle/deprecation -> resolve a name or publish a
draft -> wait for durable acknowledgement -> record the immutable input.
Main caller: Run.use_artifact. Existing objects retain their origin; descriptive
edits and input relationships use the existing journals and Foundry stream.
"""


def use_artifact(run, artifact_or_name, type=None, aliases=None, use_as=None):
    """Match the pinned branches without applying name-only checks to objects.

    Main callers: Run.use_artifact and the use_model helper. A failed draft
    upload must not create a successful input relationship. Deprecated use_as
    warns but does not change persisted behavior.
    """
    import backprop
    from .artifacts import Artifact
    from .errors import Error
    if run._settings._offline:
        raise TypeError("Cannot use artifact when in offline mode.")
    if use_as is not None:
        backprop.termwarn("`use_as` argument is deprecated and does not affect the behaviour of `run.use_artifact`", repeat=False)
    if isinstance(artifact_or_name, str):
        if run._sink is None:
            raise Error("Artifact retrieval requires an authenticated store")
        from .apis.public import Api
        run.flush()
        artifact = Api._from_store(run._sink, entity=run.entity, project=run.project, config=run._deployment).artifact(artifact_or_name, type=type)
        if type is not None and type != artifact.type:
            raise ValueError(f"Supplied type {type} does not match type {artifact.type} of artifact {artifact.name}")
    elif isinstance(artifact_or_name, Artifact):
        artifact = artifact_or_name
        if artifact.is_draft():
            if use_as is not None:
                backprop.termwarn("Indicating use_as is not supported when using a draft artifact")
            from .artifact_updates import logging_aliases, add_artifact_aliases
            from .artifacts import _validate_metadata
            from .artifact_publication import validate_collection_type
            requested = logging_aliases(aliases)
            artifact.metadata = _validate_metadata(artifact.metadata)
            if not artifact._logged:
                from .artifact_logging import log_artifact
                log_artifact(run, artifact, aliases=requested)
                artifact.wait()
            else:
                # A queued publication already owns immutable identity and
                # provenance. Wait on that journal, then publish new aliases
                # through an ordinary durable edit instead of rewriting it.
                validate_collection_type(artifact, run)
                from .artifact_handoff import publish_pending
                publish_pending(artifact, run)
                artifact.wait()
                add_artifact_aliases(artifact, requested)
                artifact.save()
        else:
            # Restored config artifacts can have only local journal storage,
            # even when an empty manifest has no files marking it as pending.
            from .artifact_handoff import publish_pending
            publish_pending(artifact, run)
        from .artifact_updates import flush_artifact_edits
        flush_artifact_edits(artifact)
    else:
        raise ValueError(
            'You must pass an artifact name (e.g. "pedestrian-dataset:v1"), '
            'an instance of `wandb.Artifact`, or `wandb.Api().artifact()` to `use_artifact`'
        )
    record_input(run, artifact)
    return artifact


def record_input(run, artifact):
    """Append an immutable relationship using the current run's durable journal.

    Main callers: explicit online use and native offline config-reference
    capture. This stage does not publish bytes or change artifact ownership.
    """
    run._emit("artifact_use", {"artifact_id": artifact._id, "name": artifact.name,
        "version": artifact.version, "artifact_type": artifact.type, "use_as": None,
        "qualified_name": artifact.qualified_name,
        "source_entity": artifact.entity, "source_project": artifact.project})


def prepare_config_artifact(run, artifact):
    """Queue local config references offline; require committed input online.

    Main caller: Run._prepare_config_artifacts. Offline configuration capture is
    a deliberate native extension: it retains local artifact identities/files
    for later sync, while explicit use_artifact retains W&B's offline error.
    """
    if not run._settings._offline:
        return run.use_artifact(artifact)
    if not artifact._logged:
        artifact._log_to_run(run)
    record_input(run, artifact)
    return artifact
