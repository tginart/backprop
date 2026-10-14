"""Replay collection edits without changing immutable artifact manifests.

Architecture: ordered publications establish stable collection identities ->
collection_update events change names/types/descriptions -> readers project the
current names onto artifact copies. Alias ownership follows the stable identity.
Main callers: artifact_views and Public ArtifactCollection. Concurrent edits to
different fields merge; competing edits to one field use canonical event order.
An occupied destination is a visible conflict, never a silent collection merge.
"""
import copy
import hashlib

from .artifact_events import iter_artifact_events
from .event_order import ordered_events


def collection_identity(entity, project, name, artifact_type):
    """Keep a collection's existing opaque ID independent of later renames."""
    return hashlib.sha256(f"{entity}/{project}/{artifact_type}/{name}".encode()).hexdigest()


def collection_catalog(events):
    """Build current records and immutable artifact-to-collection membership.

    Main callers: artifact overlay and public collection saves. A reused former
    name starts a new collection generation. A repeated artifact ID always keeps
    its original membership, including retry publications after a rename.
    """
    events = ordered_events(events)
    originals = list(iter_artifact_events(events))
    operations = ordered_events([*originals, *(e for e in events if e['type'] == 'collection_update')])
    records, routes, members = {}, {}, {}
    for event in operations:
        p = event['payload']
        scope = event.get('entity', ''), event['project']
        route = (*scope, p['name'], p['artifact_type'])
        if event['type'] == 'artifact':
            artifact = (*scope, p['artifact_id'])
            if artifact in members:
                continue
            identifier = routes.get(route)
            if identifier is None:
                identifier = collection_identity(*scope, p['name'], p['artifact_type'])
                if identifier in records:
                    identifier = hashlib.sha256(f"{identifier}/{p['artifact_id']}".encode()).hexdigest()
                records[identifier] = dict(id=identifier, entity=scope[0], project=scope[1],
                    name=p['name'], artifact_type=p['artifact_type'], description=p.get('description'),
                    tags=[], created_at=event['timestamp'], updated_at=event['timestamp'])
                routes[route] = identifier
            members[artifact] = identifier
            record = records[identifier]
            record['updated_at'] = event['timestamp']
            # Legacy collections derived tags from versions until explicitly edited.
            if not record.get('_tags_edited'):
                record['tags'] = sorted(set(record['tags']) | set(p.get('tags', [])))
            continue
        identifier = p.get('collection_id') or routes.get(route)
        if identifier not in records:
            raise ValueError('Collection update has no visible original collection')
        record = records[identifier]
        if (record['entity'], record['project']) != scope:
            raise ValueError('Collection update crosses its original project')
        values = copy.deepcopy(p['values'])
        if set(values) - {'name', 'artifact_type', 'description', 'tags'}:
            raise ValueError('Unsupported collection update field')
        destination = (*scope, values.get('name', record['name']), values.get('artifact_type', record['artifact_type']))
        if destination in routes and routes[destination] != identifier:
            raise ValueError('Collection destination already exists')
        previous = (*scope, record['name'], record['artifact_type'])
        routes.pop(previous, None)
        routes[destination] = identifier
        record.update(values)
        if 'tags' in values:
            record['_tags_edited'] = True
        record['updated_at'] = event['timestamp']
    return records, members
