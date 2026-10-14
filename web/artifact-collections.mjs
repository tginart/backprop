/** Collection identity -> ordered edits -> current artifact names and types.
 * Main caller: model.normalizeEvents. This mirrors artifact_collections.py;
 * files and immutable version IDs never change. Occupied rename destinations
 * fail visibly, while stale editors of different fields can converge.
 */
import {sha256} from './sha256.mjs';
const digest = value => sha256(new TextEncoder().encode(value));
const key = parts => JSON.stringify(parts);
export const artifactMember = (event, artifact) => key([event.entity || '', event.project, artifact.artifact_id]);

export function collectionCatalog(events) {
  const records = new Map(), routes = new Map(), members = new Map();
  for (const event of events) {
    if (!['artifact', 'collection_update'].includes(event.type)) continue;
    const p = event.payload, entity = event.entity || '', scope = [entity, event.project];
    const route = key([...scope, p.name, p.artifact_type]);
    if (event.type === 'artifact') {
      const artifact = artifactMember(event, p);
      if (members.has(artifact)) continue;
      let id = routes.get(route);
      if (id === undefined) {
        id = digest(`${entity}/${event.project}/${p.artifact_type}/${p.name}`);
        if (records.has(id)) id = digest(`${id}/${p.artifact_id}`);
        records.set(id, {id, entity, project:event.project, name:p.name, artifact_type:p.artifact_type,
          description:p.description ?? null, tags:[], created_at:event.timestamp, updated_at:event.timestamp});
        routes.set(route, id);
      }
      members.set(artifact, id);
      const record = records.get(id);
      record.updated_at = event.timestamp;
      if (!record._tags_edited) record.tags = [...new Set([...record.tags, ...(p.tags || [])])].sort();
      continue;
    }
    const id = p.collection_id || routes.get(route), record = records.get(id);
    if (!record) throw new Error('Collection update has no visible original collection');
    if (record.entity !== entity || record.project !== event.project) throw new Error('Collection update crosses its original project');
    const values = structuredClone(p.values);
    if (Object.keys(values).some(field => !['name','artifact_type','description','tags'].includes(field))) throw new Error('Unsupported collection update field');
    const destination = key([...scope, values.name ?? record.name, values.artifact_type ?? record.artifact_type]);
    if (routes.has(destination) && routes.get(destination) !== id) throw new Error('Collection destination already exists');
    routes.delete(key([...scope, record.name, record.artifact_type]));
    routes.set(destination, id);
    Object.assign(record, values);
    if (Object.hasOwn(values, 'tags')) record._tags_edited = true;
    record.updated_at = event.timestamp;
  }
  return {records, members};
}
