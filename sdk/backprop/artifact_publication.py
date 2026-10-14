"""Validate publication against the currently visible artifact collection.

Architecture: choose the immutable owner namespace -> read current manifests ->
reject a conflicting collection type. Main caller: pending Run.use_artifact.
This is a preflight check over Foundry events, not a cross-writer transaction.
"""
from .apis.public import Api


def validate_collection_type(artifact, run, *, preserve_owner=True):
    """Reject a known collection-type conflict before publishing a draft.

    Main caller: use_artifact before fresh publication or an offline handoff.
    A queued native manifest keeps its original namespace and ownership. This
    preserves stored config references when another project uses that object.
    Output logging checks the destination collection before its source checks.
    """
    entity = artifact.entity if preserve_owner and artifact._logged else run.entity
    project = artifact.project if preserve_owner and artifact._logged else run.project
    api = Api._from_store(run._sink, entity=entity, project=project, config=run._deployment)
    records = api._artifact_events(entity, project)
    conflicts = sorted({e['payload']['artifact_type'] for e in records
                        if e['payload']['name'] == artifact._name and e['payload']['artifact_type'] != artifact.type})
    if conflicts:
        raise ValueError(f"Artifact {artifact.name} already exists with type '{conflicts[0]}'; "
                         f"cannot create another with type '{artifact.type}'")
