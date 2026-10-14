/**
 * Architecture: scoped completion IDs -> immutable contributed files -> output
 * relationships for each contributor run. Journals and cached events stay
 * untouched. Late parts remain pending. Main caller: model.normalizeEvents.
 * These rules mirror sdk/backprop/artifact_lineage.py.
 */
const scopeKey = (event, manifest, group, id) => JSON.stringify([
  event.entity || '', event.project, manifest.name, manifest.artifact_type, group, id,
]);
export const contributionKey = event => scopeKey(event, event.payload.manifest,
  event.payload.distributed_id, event.payload.part_id);
const relationKey = (event, id) => JSON.stringify([event.entity || '', event.project, event.run_id, id]);

/** Find derived outputs and consumed parts without changing source envelopes.
 * Main caller: normalizeEvents after ordinary run replay. signature uses the
 * model's existing immutable-file comparison. Every contributed file is checked
 * before a relation is added, including when the run already lists the output.
 */
export function contributorArtifacts(events, signature) {
  const originals = events.filter(event => event.type === 'artifact');
  const relations = new Set(originals.map(event => relationKey(event, event.payload.artifact_id)));
  const completions = new Map(), consumed = new Set(), outputs = [];
  for (const event of originals) {
    const payload = event.payload;
    if (payload._backprop_distributed || payload._backprop_incremental) continue;
    const ids = 'contribution_ids' in payload ? payload.contribution_ids : [];
    if (!Array.isArray(ids) || ids.some(id => typeof id !== 'string' || !id)) {
      throw new Error('Completed distributed artifact has invalid contribution IDs');
    }
    if (ids.length && typeof payload.distributed_id !== 'string') {
      throw new Error('Completed distributed artifact has invalid distributed group');
    }
    for (const id of ids) {
      const key = scopeKey(event, payload, payload.distributed_id, id), previous = completions.get(key);
      if (previous && previous.payload.artifact_id !== payload.artifact_id) {
        throw new Error('Distributed contribution belongs to conflicting completed artifacts');
      }
      if (!previous) completions.set(key, event);
    }
  }
  for (const part of events.filter(event => event.type === 'artifact_part')) {
    const key = contributionKey(part), completion = completions.get(key);
    if (!completion) continue;
    const files = new Map(completion.payload.files.map(file => [file.name, file]));
    for (const file of part.payload.manifest.files) {
      const selected = files.get(file.name);
      if (!selected || signature({files:[selected]}) !== signature({files:[file]})) {
        throw new Error('Completed distributed artifact changes an original contribution file');
      }
    }
    consumed.add(key);
    const relation = relationKey(part, completion.payload.artifact_id);
    if (relations.has(relation)) continue;
    relations.add(relation);
    outputs.push({...completion, run_id:part.run_id, source_event_id:part.event_id,
      source_event_type:'artifact_part', source_completion_event_id:completion.event_id});
  }
  return {outputs, consumed};
}
