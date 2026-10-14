"""Derive completed output relationships from durable contribution records.

Architecture: scoped completion IDs -> matching immutable parts -> verified file
membership -> synthetic read envelopes for each contributor run. The underlying
journals remain unchanged. Late parts stay pending until their own completion.
Main caller: artifact_events.iter_artifact_events. The browser applies the same
rules in artifact-lineage.mjs. No writer, scheduler or additional storage is used.
"""
import json
import uuid


def contribution_key(event, manifest, group, identity):
    """Scope a part to its entity, project, collection, type and group.

    Main caller: contributor_events. An ID appearing in another namespace or
    collection cannot consume this part or grant its run an output relationship.
    """
    return (event.get('entity',''), event['project'], manifest['name'],
            manifest['artifact_type'], group, identity)


def validate_files(part, completion):
    """Require every contributed file to occur unchanged in the completed union.

    Main callers: contributor_events and a pending Artifact.wait. Logical byte
    identities ignore verified storage locations and primary-assigned origins,
    using the same immutable-content comparison as ordinary artifact readers.
    """
    from .artifact_events import manifest_identity
    files={ref['name']:ref for ref in completion['payload']['files']}
    def identity(reference):
        return manifest_identity({**part,'payload':{'files':[reference]}})
    for reference in part['payload']['manifest']['files']:
        selected=files.get(reference['name'])
        if selected is None or identity(selected)!=identity(reference):
            raise ValueError('Completed distributed artifact changes an original contribution file')


def contributor_events(parts, originals):
    """Return missing contributor relationships after deterministic validation.

    Main caller: iter_artifact_events after collecting all explicit and embedded
    manifests. Existing relationships win, including the primary's own output.
    A repeated completion may enrich lineage, but cannot consume a part twice
    into different immutable artifact IDs. Synthetic records are never written.
    """
    from .event_order import ordered_events
    completions={}
    relations={(event.get('entity',''),event['project'],event['run_id'],event['payload'].get('artifact_id'))
               for event in originals}
    for event in originals:
        payload=event['payload']
        if payload.get('_backprop_distributed') or payload.get('_backprop_incremental'):
            continue
        identities=payload.get('contribution_ids',[])
        if not isinstance(identities,list) or any(not isinstance(value,str) or not value for value in identities):
            raise ValueError('Completed distributed artifact has invalid contribution IDs')
        if identities and not isinstance(payload.get('distributed_id'),str):
            raise ValueError('Completed distributed artifact has invalid distributed group')
        for identity in identities:
            key=contribution_key(event,payload,payload['distributed_id'],identity)
            previous=completions.get(key)
            if previous is not None and previous['payload']['artifact_id']!=payload['artifact_id']:
                raise ValueError('Distributed contribution belongs to conflicting completed artifacts')
            completions.setdefault(key,event)
    derived=[]
    for part in ordered_events(parts):
        payload=part['payload']
        key=contribution_key(part,payload['manifest'],payload['distributed_id'],payload['part_id'])
        completion=completions.get(key)
        if completion is None:
            continue
        validate_files(part,completion)
        identity=completion['payload']['artifact_id']
        relation=(part.get('entity',''),part['project'],part['run_id'],identity)
        if relation in relations:
            continue
        relations.add(relation)
        event_id=uuid.uuid5(uuid.NAMESPACE_URL,'backprop:contributor:'+json.dumps(relation,separators=(',',':'))).hex
        derived.append({**completion,'event_id':event_id,'run_id':part['run_id'],
            'source_event_id':part['event_id'],'source_event_type':'artifact_part',
            'source_completion_event_id':completion['event_id']})
    return derived
