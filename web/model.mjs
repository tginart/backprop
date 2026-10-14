/**
 * Architecture: Foundry envelopes -> deduplicated run state -> chart/table views.
 * No network or DOM lives here. Rebuilding from the same events is deterministic.
 * Run identity includes entity and project. Incremental table rows use absolute
 * offsets; replaying a published event never appends its rows a second time.
 * Main callers: app.mjs after api.loadWorkspace; frontend model tests.
 */
import { smoothSeries } from './smoothing.mjs';
import { incompleteWriters } from './history-completeness.mjs';
import { contributorArtifacts, contributionKey } from './artifact-lineage.mjs';
import { TensorBoardHistory } from './tensorboard-replay.mjs';
import { orderGroupRows, groupedSummaries, applyGroupSummary } from './tensorboard-summary.mjs';
import { collectionCatalog, artifactMember } from './artifact-collections.mjs';
export const COLORS = ['#8abbff', '#6ac9d0', '#b49ae8', '#fbb360', '#4c90f0', '#ee8f9b', '#8ad5e3', '#d9b86c'];
export const finite = value => typeof value === 'number' && Number.isFinite(value);
export const runKey = event => [event.entity || '', event.project || 'uncategorized', event.run_id || event.id].join('/');
export const projectKey = run => [run.entity || '', run.project || 'uncategorized'].join('/');
export const isRich = value => value && typeof value === 'object' && Boolean(value._type);

// ISO event timestamps retain microseconds, including timezone-offset inputs.
// Use the same instant/event-ID ordering as Python rather than locale collation.
const lexical = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const instant = value => {
  const millis = Date.parse(value);
  if (!Number.isFinite(millis)) throw new Error('Invalid event timestamp');
  const fractional = String(value).match(/\.(\d+)(?:Z|[+-]\d\d:\d\d)$/)?.[1] || '';
  return BigInt(millis) * 1000n + BigInt(fractional.padEnd(6, '0').slice(3, 6));
};
const compareEvents = (a, b) => lexical(instant(a.timestamp), instant(b.timestamp)) || lexical(String(a.event_id || ''), String(b.event_id || ''));

// Object member ordering is not part of JSON event identity. Python dictionaries
// compare by keys and values too, including nested objects inside arrays.
const eventJSON = value => JSON.stringify(value, (_key, item) =>
  item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);

/** Build the current workspace without mutating API cache entries.
 * Why this exists: stream records and archived envelopes carry identical data
 * in slightly different wrappers. This is their one shared interpretation.
 * Main callers: app.mjs refresh().
 */
export function normalizeEvents(input) {
  const seen = new Set(), identities = new Map();
  const runs = new Map();
  const issues = [];
  const globalIssues = [], issuesByProject = Object.create(null);
  // Keep unknown formats explicit, but only warn in their owning project.
  // Undecodable records without project identity remain workspace-wide.
  const addIssues = (event, messages) => {
    issues.push(...messages);
    const target = typeof event?.project === 'string'
      ? (issuesByProject[projectKey(event)] ||= []) : globalIssues;
    target.push(...messages);
  };
  const events = input.map((raw, index) => {
    let event;
    try {
      event = raw.value || raw;
      // Workspace display revisions share transport, never experiment identity.
      if ((event.type || event.event_type) === 'workspace_snapshot') return null;
      return { ...event, type: event.type || event.event_type,
        payload: typeof event.payload === 'string' ? JSON.parse(event.payload) : (event.payload || {}), _index: index };
    } catch { addIssues(event, ['An event could not be decoded.']); return null; }
  }).filter(Boolean).sort(compareEvents);
  const importedHistory = new TensorBoardHistory(events);

  for (const event of events) {
    if (event.event_id && seen.has(event.event_id)) {
      const { _index, ...record } = event;
      if (identities.get(event.event_id) !== eventJSON(record)) throw new Error(`Conflicting records for event ID: ${event.event_id}`);
      continue;
    }
    const { _index, ...record } = event;
    identities.set(event.event_id, eventJSON(record));
    if (event.event_id) seen.add(event.event_id);
    if (['project_create', 'collection_update', 'custom_chart'].includes(event.type)) continue;
    if (!event.run_id) { addIssues(event, ['An event is missing its run ID.']); continue; }
    const key = runKey(event);
    if (!runs.has(key)) runs.set(key, { key, id: event.run_id, entity: event.entity || '', project: event.project || 'uncategorized', name: event.run_id,
      state: 'running', config: Object.create(null), summary: Object.create(null), tags: [], pinned_config_keys: [], group: '', job_type: '', history: [], definitions: Object.create(null), rich: Object.create(null), artifacts: [], used_artifacts: [], files: [], console: [],
      writers: Object.create(null), writer_rich: Object.create(null), pending_artifacts: [], system: [], _publicSummary: Object.create(null), _publicDeleted: new Set(), _summarySnapshots: new Map(), control: null, stop_requests: [], _controlEvents: [], _writerEvents: [],
      created_at: event.timestamp, _startedAt: null, updated_at: event.timestamp, eventCount: 0, _steps: new Map(), _tables: new Map(), _stats: new Map(), _artifactKeys: new Map() });
    const run = runs.get(key);
    const eventIssues = [];
    const p = event.payload, writer = p._writer;
    if (writer) {
      run._writerEvents.push(event);
      run.writers[writer.id] ||= { ...writer, state: 'running' };
      if (['run_finish', 'writer_finish'].includes(event.type)) run.writers[writer.id].state = p.state || 'finished';
    }
    const applySnapshot = values => {
      const id = writer?.id || 'single', prior = run._summarySnapshots.get(id) || {};
      for (const [key, value] of Object.entries(values || {})) {
        if (!Object.hasOwn(prior, key) || JSON.stringify(prior[key]) !== JSON.stringify(value)) run.summary[key] = value;
      }
      run._summarySnapshots.set(id, values || {});
    };
    run.updated_at = event.timestamp || run.updated_at;
    run.eventCount++;
    // Table outputs share the values' atomic event instead of requiring a
    // separate artifact event. Repeated summaries retain immutable identity.
    if (['run_start', 'history', 'summary', 'config'].includes(event.type) && p.artifacts !== undefined) {
      if (!Array.isArray(p.artifacts)) eventIssues.push('An event contains an invalid artifact manifest list.');
      else for (const artifact of p.artifacts) appendArtifact(run, artifact, event.timestamp, eventIssues);
    }
    switch (event.type) {
      case 'run_start':
        if (run._startedAt === null) run.created_at = run._startedAt = event.timestamp;
        if (writer && !writer.primary && Object.values(run.writers).some(w => w.primary)) { Object.assign(run.config, p.config || {}); break; }
        for (const field of ['name', 'group', 'job_type', 'notes', 'tags']) if (p[field] !== undefined) run[field] = p[field];
        Object.assign(run.config, p.config || {});
        run.state = p.state || 'running';
        run.control = p.control?.stop_supported === true && typeof p.control.instance_id === 'string' && p.control.instance_id
          ? { ...p.control } : null;
        break;
      case 'run_state': run.state = p.state; break;
      case 'run_edit':
        Object.assign(run.config, p.config?.values || {});
        for (const key of p.config?.deleted || []) delete run.config[key];
        for (const field of ['name', 'group', 'job_type', 'notes', 'tags', 'description']) {
          if (p.metadata?.values && Object.hasOwn(p.metadata.values, field)) run[field] = p.metadata.values[field];
          if (p.metadata?.deleted?.includes(field)) run[field] = field === 'tags' ? [] : '';
        }
        break;
      case 'writer_finish': break;
      case 'artifact_part': run.pending_artifacts.push({...p, timestamp:event.timestamp}); break;
      case 'system': {
        // System reads need the same time and writer identity as SDK history.
        // Keep nested metrics intact for browser path handling and preserve
        // explicit metric timestamps, as the Python reader does.
        const row = { ...p.metrics, _step: p.step ?? run.system.length };
        if (writer) Object.assign(row, { _writer: writer.id, _writer_label: writer.label });
        if (!Object.hasOwn(row, '_timestamp')) row._timestamp = Number(instant(event.timestamp)) / 1e6;
        if (run._startedAt !== null && !Object.hasOwn(row, '_runtime')) row._runtime = row._timestamp - Number(instant(run._startedAt)) / 1e6;
        run.system.push(row);
        break;
      }
      case 'artifact_update': break; // Project-wide overlay applied after all manifests are known.
      case 'artifact_use': run.used_artifacts.push(structuredClone(p)); break;
      case 'run_metadata':
        for (const field of ['name', 'group', 'job_type', 'notes', 'tags']) {
          if (p.values?.[field] !== undefined) run[field] = p.values[field];
          if (p.deleted?.includes(field)) run[field] = field === 'tags' ? [] : '';
        }
        if (Array.isArray(p.values?.pinned_config_keys)) run.pinned_config_keys = p.values.pinned_config_keys.filter(key => typeof key === 'string');
        if (p.deleted?.includes('pinned_config_keys')) run.pinned_config_keys = [];
        break;
      case 'config':
        Object.assign(run.config, p.values || p.config || {});
        for (const key of p.deleted || []) delete run.config[key];
        if (p.annotation_labels !== undefined) {
          try { applyAnnotationLabels(run.config, p.annotation_labels); }
          catch (error) { eventIssues.push(error.message); }
        }
        break;
      case 'summary':
        if (p.public_edit) {
          Object.assign(run._publicSummary, p.values);
          for (const key of Object.keys(p.values || {})) run._publicDeleted.delete(key);
          for (const key of p.deleted || []) { delete run._publicSummary[key]; run._publicDeleted.add(key); }
        }
        if (p.automatic) { applySnapshot(p.values); break; }
        Object.assign(run.summary, p.values || p.summary || {});
        for (const key of p.deleted || []) delete run.summary[key];
        break;
      case 'metric_definition': run.definitions[p.name] = p; break;
      case 'history_truncate': {
        run.history = run.history.filter(row => row._step <= p.step);
        run._steps = new Map(run.history.map(row => [row._step, row]));
        rebuildHistoryViews(run, eventIssues);
        run.summary = Object.assign(Object.create(null), p.summary || {});
        break;
      }
      case 'history': {
        for (const metrics of importedHistory.rows(event)) {
          const step = p.step ?? p.metrics?._step ?? run.history.length;
          const stepKey = metrics._tensorboard_group_members
            ? `tensorboard:${metrics._tensorboard_record}` : writer ? `${writer.id}:${step}` : step;
          const row = run._steps.get(stepKey) || Object.assign(Object.create(null), { _step: step, _timestamp: Number(instant(event.timestamp)) / 1e6, ...(writer ? {_writer:writer.id, _writer_label:writer.label} : {}) });
          for (const [name, original] of Object.entries(metrics)) {
            const value = accumulateValue(original, run._tables, eventIssues, `${writer?.id || 'single'}:${name}`);
            // Rich history keeps only original deltas. Latest views are
            // materialized once; older snapshots are rebuilt only on demand.
            row[name] = original;
            if (!name.startsWith('_')) recordLeaves(run, name, value, step, event.timestamp, writer);
          }
          // Match public SDK history for ordinary and imported rows alike:
          // preserve explicit training times, otherwise derive elapsed time
          // from the same microsecond event timestamp and first actual start.
          // A fork may replay inherited history before its own run_start.
          if (run._startedAt !== null && !Object.hasOwn(row, '_runtime'))
            row._runtime = row._timestamp - Number(instant(run._startedAt)) / 1e6;
          if (!run._steps.has(stepKey)) run.history.push(row);
          run._steps.set(stepKey, row);
          if (p.summary && typeof p.summary === 'object') {
            if (writer) applySnapshot(p.summary);
            else run.summary = Object.assign(Object.create(null), p.summary);
          }
          if (metrics._tensorboard_incomplete) eventIssues.push('TensorBoard source observations are incomplete.');
        }
        break;
      }
      case 'artifact': appendArtifact(run, p, event.timestamp, eventIssues); break;
      case 'file': {
        const previous = run.files.findIndex(file => file.name === p.name);
        const file = { ...p.file, name: p.name, policy: p.policy, timestamp: event.timestamp };
        if (previous < 0) run.files.push(file); else run.files[previous] = file;
        break;
      }
      case 'console': applyConsole(run.console, p, event.timestamp, eventIssues); break;
      case 'run_stop_request':
      case 'run_stop_ack': run._controlEvents.push(event); break;
      case 'finish':
      case 'run_finish': run.state = p.state || (p.exit_code ? 'failed' : 'finished'); run.finished_at = event.timestamp; break;
      default: eventIssues.push(`Unsupported event type: ${String(event.type)}`);
    }
    addIssues(event, eventIssues);
  }
  // Alias ownership belongs to a project/collection, including artifacts emitted
  // by different runs. Apply updates to each visible relationship after replay.
  const aliases = new Map(), overlays = new Map(), knownArtifacts = new Set();
  const artifactEvents = events.flatMap(event => [...(event.payload.artifacts || []).map(payload => ({...event, type:'artifact', payload})), event]);
  const collections = collectionCatalog(artifactEvents);
  const contributions = contributorArtifacts(artifactEvents, artifactContentSignature);
  for (const event of contributions.outputs) {
    const run = runs.get(runKey(event)), eventIssues = [];
    appendArtifact(run, event.payload, event.timestamp, eventIssues);
    addIssues(event, eventIssues);
  }
  for (const event of artifactEvents) {
    const p = event.payload, scope = JSON.stringify([event.entity || '', event.project, collections.members.get(artifactMember(event, p))]);
    const identity = JSON.stringify([event.entity || '', event.project, p.artifact_id]);
    if (event.type === 'artifact' && !knownArtifacts.has(identity)) {
      knownArtifacts.add(identity);
      for (const alias of p.aliases || []) aliases.set(JSON.stringify([scope, alias]), identity);
    } else if (event.type === 'artifact_update') {
      overlays.set(identity, { ...(overlays.get(identity) || {}), ...p.values });
      for (const alias of p.alias_remove || []) {
        const key = JSON.stringify([scope, alias]);
        if (aliases.get(key) === identity) aliases.delete(key);
      }
      for (const alias of p.alias_add || []) aliases.set(JSON.stringify([scope, alias]), identity);
    }
  }
  for (const run of runs.values()) for (const artifact of run.artifacts) {
    const identity = JSON.stringify([run.entity, run.project, artifact.artifact_id]);
    Object.assign(artifact, overlays.get(identity) || {});
    const collection = collections.records.get(collections.members.get(artifactMember(run, artifact)));
    if (collection) Object.assign(artifact, {name:collection.name, artifact_type:collection.artifact_type, _collection_id:collection.id});
    artifact.aliases = [...aliases].filter(([, owner]) => owner === identity).map(([key]) => JSON.parse(key)[1]).sort();
  }
  for (const run of runs.values()) for (const artifact of run.used_artifacts) {
    const source = {entity:artifact.source_entity ?? run.entity, project:artifact.source_project ?? run.project};
    const collection = collections.records.get(collections.members.get(artifactMember(source, artifact)));
    if (collection) {
      artifact.name = collection.name + (artifact.version ? ':' + artifact.version : '');
      artifact.artifact_type = collection.artifact_type;
      artifact.qualified_name = [source.entity, source.project, artifact.name].join('/');
    }
  }
  const corrections = groupedSummaries(events, importedHistory, runKey);
  const values = [...runs.values()].sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
  values.forEach(run => {
    run.pending_artifacts = [...new Map(run.pending_artifacts.filter(part => !contributions.consumed.has(contributionKey({...run, payload:part}))).map(part => [part.part_id, part])).values()];
    // Request timestamps come from another computer. Match the latest SDK
    // instance first, then fold acknowledgments by identity so clock skew cannot
    // discard a valid acknowledgment or let a later request undo acceptance.
    const stops = new Map();
    for (const event of run._controlEvents) {
      const p = event.payload, id = event.type === 'run_stop_request' ? event.event_id : p.request_id;
      if (!run.control || p.instance_id !== run.control.instance_id || typeof id !== 'string' || !id) continue;
      if (event.type === 'run_stop_request' && p.action !== 'stop' || event.type === 'run_stop_ack' && !['accepted', 'failed'].includes(p.status)) continue;
      const previous = stops.get(id), status = event.type === 'run_stop_ack' ? p.status : 'requested';
      if (previous && ({ requested: 0, accepted: 1, failed: 2 }[previous.status] > { requested: 0, accepted: 1, failed: 2 }[status])) continue;
      stops.set(id, { request_id: id, instance_id: p.instance_id, status, timestamp: event.timestamp,
        ...(status === 'failed' ? { error: typeof p.error === 'string' ? p.error : 'The stop callback failed.' } : {}) });
    }
    run.stop_requests = [...stops.values()];
    run.history_incomplete_writers = incompleteWriters(run.writers, run._writerEvents);
    delete run._writerEvents;
    if (!Object.keys(run.writers).length) run.history.sort((a, b) => a._step - b._step);
    orderGroupRows(run.history);
    if (corrections.has(run.key)) {
      const replayIssues = [];
      const persistedSummary = structuredClone(run.summary);
      rebuildHistoryViews(run, replayIssues);
      run.summary = persistedSummary;
      addIssues(run, replayIssues);
    }
    applyGroupSummary(run.summary, corrections.get(run.key));
    Object.assign(run.summary, run._publicSummary);
    for (const key of run._publicDeleted) delete run.summary[key];
    delete run._publicSummary; delete run._publicDeleted;
    const hash = [...run.key].reduce((number, char) => ((number * 31) + char.charCodeAt(0)) >>> 0, 0);
    run.color = COLORS[hash % COLORS.length];
    delete run._summarySnapshots; delete run._steps; delete run._tables; delete run._stats; delete run._artifactKeys; delete run._controlEvents; delete run._startedAt;
  });
  // Explicit evaluations can outlive or bypass experiment logging. A disabled
  // record must not look like a live training process while its files are shown.
  const started = new Set(events.filter(event => event.type === 'run_start').map(runKey));
  const disabled = new Set(events.filter(event => event.type === 'artifact' && event.payload.logging_disabled).map(runKey));
  for (const run of values) if (disabled.has(run.key) && !started.has(run.key)) run.state = 'disabled';
  const projects = [...new Map([...values, ...events.filter(event => event.type === 'project_create')].map(run => [projectKey(run), { id: projectKey(run), name: run.project, entity: run.entity || '' }])).values()];
  return { runs: values, projects, issues: [...new Set(issues)], globalIssues: [...new Set(globalIssues)],
    issuesByProject: Object.fromEntries(Object.entries(issuesByProject).map(([key, values]) => [key, [...new Set(values)]])), eventCount: seen.size || events.length };
}

/** Keep each emitted immutable artifact version once inside its owning run.
 * Main callers: explicit artifact events and Table manifests embedded in
 * initialization/history/config/summary. Names can repeat across snapshots; identity cannot be
 * shared through a global cache across projects or runs. The first manifest
 * stays visible, but conflicting immutable content produces a scoped warning.
 */
function appendArtifact(run, manifest, timestamp, issues) {
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest) || typeof manifest.name !== 'string' || !Array.isArray(manifest.files)) {
    issues.push('An event contains an invalid artifact manifest.');
    return;
  }
  const identity = typeof manifest.artifact_id === 'string' && manifest.artifact_id
    ? JSON.stringify([manifest.artifact_id, manifest.version ?? manifest.digest ?? null]) : null;
  if (identity) {
    const signature = artifactContentSignature(manifest);
    if (run._artifactKeys.has(identity)) {
      const previous = run._artifactKeys.get(identity), first = run.artifacts[previous.index];
      const enriched = previous.signature === signature ? mergeArtifactOrigins(first.files, manifest.files) : null;
      if (!enriched) issues.push(`Conflicting immutable artifact manifest: ${manifest.name} (${manifest.artifact_id}, ${String(manifest.version ?? manifest.digest ?? 'unversioned')}). Showing the first recorded snapshot.`);
      else first.files = enriched;
      return;
    }
    run._artifactKeys.set(identity, { signature, index: run.artifacts.length });
  }
  run.artifacts.push({ ...manifest, files: manifest.files.map(file => ({ ...file })), timestamp });
}

/** Enrich only historically unknown provenance while keeping the first route.
 * Main callers: matching immutable manifest repeats. Explicit ref:null means a
 * nonreference file; birth:null means unknown. Validate the whole merge before
 * changing visible state so a conflict cannot leave half-applied provenance.
 */
function mergeArtifactOrigins(first, second) {
  const source = new Map(second.map(file => [file.name || file.path, file])), merged = [];
  for (const file of first) {
    const other = source.get(file.name || file.path), next = { ...file };
    if (!other) return null;
    for (const field of ['ref', 'birth_artifact_id']) {
      if (!Object.hasOwn(other, field) || field === 'birth_artifact_id' && other[field] === null) continue;
      if (!Object.hasOwn(next, field) || field === 'birth_artifact_id' && next[field] === null) next[field] = other[field];
      else if (next[field] !== other[field]) return null;
    }
    merged.push(next);
  }
  return merged;
}

/** Compare logical artifact content, independent of object insertion order.
 * Main callers: appendArtifact. File order and publication lifecycle fields do
 * not change a manifest. Equal valid SHA256, size and a logical file name permit
 * another storage location; without that byte identity, routing fields remain
 * part of the comparison. MIME and upload MD5 may vary with transport, while
 * artifact digest and other metadata must agree. Provenance merges separately.
 * Arrays inside user metadata retain their meaningful order.
 */
function artifactContentSignature(manifest) {
  const canonical = value => Array.isArray(value) ? value.map(canonical)
    : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
  const files = manifest.files.map(file => {
    if (!file || typeof file !== 'object' || Array.isArray(file)) return JSON.stringify(canonical(file));
    const content = { ...file };
    delete content.ref; delete content.birth_artifact_id;
    if (typeof file.name === 'string' && file.name && /^[a-f0-9]{64}$/i.test(file.sha256 || '') && Number.isSafeInteger(file.size) && file.size >= 0) {
      content.sha256 = file.sha256.toLowerCase();
      for (const field of ['dataset_rid', 'path', 'branch', '_backprop_local_file', 'mime_type', 'md5']) delete content[field];
    }
    return JSON.stringify(canonical(content));
  }).sort();
  const content = Object.fromEntries(Object.entries(manifest).filter(([key]) => !['files', 'created_at', 'history_step', 'state', '_writer'].includes(key)));
  return JSON.stringify(canonical({ ...content, files }));
}

/** Apply terminal edits to the accumulated text of one output stream.
 * Main callers: console event replay. Python counts Unicode characters, so
 * Array.from preserves surrogate pairs while edits span multiple log chunks.
 * Other streams and the timestamps of surviving chunks remain unchanged.
 */
function applyConsole(chunks, payload, timestamp, issues) {
  let remaining = payload.replace_tail ?? 0;
  if (!Number.isSafeInteger(remaining) || remaining < 0) { issues.push('A console event has an invalid replacement length.'); return; }
  const stream = payload.stream || 'stdout';
  for (let index = chunks.length - 1; index >= 0 && remaining; index--) {
    if (chunks[index].stream !== stream) continue;
    const characters = Array.from(chunks[index].text);
    const removed = Math.min(remaining, characters.length);
    remaining -= removed;
    if (removed === characters.length) chunks.splice(index, 1);
    else chunks[index].text = characters.slice(0, characters.length - removed).join('');
  }
  if (remaining) issues.push('A console edit exceeded the available stream text.');
  if (payload.text) chunks.push({ text: payload.text, stream, timestamp });
}

/** Rebuild derived rich values and aggregates after an explicit SDK rewind.
 * Main callers: history_truncate replay. Original history keeps table deltas,
 * so truncating only the latest table would leave removed rows behind.
 */
function rebuildHistoryViews(run, issues) {
  run._tables = new Map(); run._stats = new Map(); run.rich = Object.create(null);
  run.writer_rich = Object.create(null);
  for (const row of run.history) for (const [name, original] of Object.entries(row)) {
    if (name.startsWith('_')) continue;
    const writer = row._writer ? {id:row._writer, label:row._writer_label} : undefined;
    const value = accumulateValue(original, run._tables, issues, `${row._writer || 'single'}:${name}`);
    recordLeaves(run, name, value, row._step, new Date(row._timestamp * 1000).toISOString(), writer);
  }
}

function recordLeaves(run, name, value, step, timestamp, writer) {
  for (const [key, leaf] of flatten({[name]:value})) {
    updateSummary(run, key, leaf);
    if (isRich(leaf) || Array.isArray(leaf) || typeof leaf === 'string') {
      run.rich[key] = { value: leaf, step, timestamp };
      if (writer) (run.writer_rich[writer.id] ||= Object.create(null))[key] = {...run.rich[key], writer:writer.id, writer_label:writer.label};
    }
  }
}

export function definitionFor(run, metric) {
  metric = keyPath(metric).join('.');
  const prefixes = Object.entries(run.definitions).filter(([name]) => name.endsWith('*') && metric.startsWith(name.slice(0, -1))).sort(([a], [b]) => a.length - b.length);
  return Object.assign(Object.create(null), ...prefixes.map(([, definition]) => definition), run.definitions[metric] || {});
}

function updateSummary(run, name, value) {
  const definition = definitionFor(run, name), summaries = (definition.summary || 'last').split(',').map(item => item.trim());
  if (summaries.includes('none')) return;
  if (finite(value)) {
    const stats = run._stats.get(name) || { count: 0, sum: 0, first: value, min: value, max: value };
    stats.count++; stats.sum += value; stats.last = value; stats.min = Math.min(stats.min, value); stats.max = Math.max(stats.max, value);
    run._stats.set(name, stats);
    if (definition.summary) {
      setValueAtKey(run.summary, name, Object.fromEntries(summaries.map(item => {
        const field = item === 'best' ? (definition.goal === 'minimize' ? 'min' : 'max') : item;
        return [item, field === 'mean' ? stats.sum / stats.count : field === 'copy' ? value : stats[field]];
      })));
      return;
    }
  }
  setValueAtKey(run.summary, name, value);
}

function accumulateValue(value, tables, issues, label) {
  if (!value || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map((item, index) => accumulateValue(item, tables, issues, `${label}.${index}`));
  if (value._type === 'table') {
    const key = value.table_id || label, previous = tables.get(key);
    const incremental = String(value.log_mode || '').toLowerCase() === 'incremental';
    const data = incremental && previous ? previous.data : [];
    const missing = new Set(incremental ? previous?._missing_rows || [] : []);
    const offset = value.row_offset ?? (incremental ? data.length : 0);
    if (offset > data.length) {
      issues.push(`Table ${label} has missing rows before offset ${offset}.`);
      while (data.length < offset) { missing.add(data.length); data.push(Array((value.columns || []).length).fill(null)); }
    }
    for (const [index, item] of (value.data || []).entries()) {
      data[offset + index] = accumulateValue(item, tables, issues, `${label}.${offset + index}`);
      missing.delete(offset + index);
    }
    const result = { ...value, data, _missing_rows: [...missing] };
    tables.set(key, result);
    if (value.related_tables) result.related_tables = Object.fromEntries(Object.entries(value.related_tables)
      .map(([id, target]) => [id, accumulateValue(target, tables, issues, `${label}.related_tables.${id}`)]));
    return result;
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, accumulateValue(item, tables, issues, `${label}.${key}`)]));
}

/** Materialize one table snapshot when a person selects a history step.
 * Main callers: app rich-data history selector. Storing only incremental rows
 * prevents a long-running rollout table from consuming quadratic memory.
 */
export function tableAtStep(run, key, step, writer) {
  let latest = null;
  const tables = new Map();
  for (const row of run.history) {
    if (row._step > step || writer && row._writer !== writer) continue;
    const value = valueAtKey(row, key);
    if (value !== undefined) latest = accumulateValue(value, tables, [], key);
  }
  return latest;
}

export function metricKeys(runs, includeHidden = false) {
  return [...new Set(runs.flatMap(run => run.history.flatMap(row => flatten(row).filter(([key, value]) => !key.startsWith('_') && finite(value) && (includeHidden || !definitionFor(run, key).hidden)).map(([key]) => key))))].sort();
}

/** Keep native path components separate while displaying familiar dotted paths.
 * Main callers: history discovery, config/summary grids and snapshot lookup.
 * Literal dots/backslashes are escaped, so later collisions never rename or
 * overwrite a metric. Rich descriptors and arrays are terminal values.
 */
export const pathKey = path => path.map(part => part.replace(/\\/g, '\\\\').replace(/\./g, '\\.')).join('.');
export function keyPath(key) {
  const path = []; let part = '', escaped = false;
  for (const char of key) {
    if (escaped) { part += char; escaped = false; }
    else if (char === '\\') escaped = true;
    else if (char === '.') { path.push(part); part = ''; }
    else part += char;
  }
  if (escaped) part += '\\';
  return [...path, part];
}
export function valueAtKey(object, key) {
  return keyPath(key).reduce((value, part) => value && typeof value === 'object' && Object.hasOwn(value, part) ? value[part] : undefined, object);
}
function setValueAtKey(object, key, value) {
  const path = keyPath(key); let target = object;
  for (const part of path.slice(0, -1)) {
    const existing = Object.hasOwn(target, part) ? target[part] : undefined;
    target[part] = existing && typeof existing === 'object' && !Array.isArray(existing) && !isRich(existing) ? Object.assign(Object.create(null), existing) : Object.create(null);
    target = target[part];
  }
  target[path.at(-1)] = value;
}
export function flatten(object) {
  const result = [];
  const visit = (value, path) => {
    if (value && typeof value === 'object' && !Array.isArray(value) && !isRich(value)) {
      for (const [key, child] of Object.entries(value)) visit(child, [...path, key]);
    } else result.push([pathKey(path), value]);
  };
  for (const [key, value] of Object.entries(object || {})) visit(value, [key]);
  return result;
}
function nativeValue(object, name) {
  if (!object || typeof object !== 'object') return undefined;
  if (Object.hasOwn(object, name)) return object[name];
  const dot = name.indexOf('.');
  return dot < 0 ? undefined : nativeValue(object[name.slice(0, dot)], name.slice(dot + 1));
}

/** Select paired numeric coordinates and apply exponential moving smoothing.
 * Main callers: charts.mjs metric panel; CSV export; model tests.
 * Logged x and y must appear in the same history row. Missing x is not guessed.
 */
export function seriesFor(run, metric, { xAxis = '_step', smoothing = 0, smoothingMethod = 'ema', logScale = false } = {}) {
  const definition = definitionFor(run, metric);
  const axis = xAxis === 'defined' ? (definition?.step_metric || '_step') : xAxis;
  let gap = false;
  const points = run.history.map(row => {
    const x = axis === '_runtime' ? row._runtime ?? row._timestamp - Date.parse(run.created_at) / 1000 : xAxis === 'defined' ? nativeValue(row, axis) : valueAtKey(row, axis);
    const y = valueAtKey(row, metric);
    if (!finite(x) || !finite(y) || (logScale && y <= 0)) { if (y !== undefined) gap = true; return null; }
    const point = { x, y, raw: y, step: row._step, ...(gap ? {breakBefore:true} : {}) };
    gap = false;
    return point;
  }).filter(Boolean).sort((a, b) => a.x - b.x);
  return smoothSeries(points,{smoothing,smoothingMethod});
}

export function formatNumber(number) {
  if (!finite(number)) return '·';
  if (number === 0) return '0';
  if (Math.abs(number) >= 1e6) return `${(number / 1e6).toFixed(2).replace(/\.00$/, '')}M`;
  if (Math.abs(number) >= 1e4) return `${(number / 1e3).toFixed(1).replace(/\.0$/, '')}k`;
  if (Math.abs(number) < 0.001) return number.toExponential(2);
  return new Intl.NumberFormat('en', { maximumFractionDigits: Math.abs(number) < 1 ? 4 : 3 }).format(number);
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

export function csv(rows) {
  return rows.map(row => row.map(value => `"${(value && typeof value === 'object' ? JSON.stringify(value) : String(value ?? '')).replaceAll('"', '""')}"`).join(',')).join('\r\n');
}

/** Include rich values assigned only to Summary without duplicating history.
 * Main callers: Logged data. Summary deletion/replacement takes effect on the
 * next render; these entries never invent a history step or old snapshots.
 */
export function loggedValues(run) {
  const sets = Object.keys(run.writer_rich || {}).length ? Object.values(run.writer_rich) : [run.rich];
  const history = sets.flatMap(values => Object.entries(values).map(([key, value]) => ({ key, ...value, source: 'history' })));
  const summary = flatten(run.summary).filter(([key, value]) => !(key in run.rich) && (isRich(value) || Array.isArray(value)))
    .map(([key, value]) => ({ key, value, source: 'summary', timestamp: run.updated_at }));
  return [...history, ...summary];
}

/** Merge a named annotation layer in the same order as Python replay.
 * Main caller: config-event reduction. Computed own keys preserve literal layer
 * names, including __proto__, without assigning to inherited object setters.
 */
function applyAnnotationLabels(config, entry) {
  const mapping=value=>value!==null && typeof value==='object' && !Array.isArray(value);
  if (!mapping(entry) || !['mask/class_labels','bounding_box/class_labels'].includes(entry.type)
      || typeof entry.key!=='string' || !mapping(entry.value) || Object.values(entry.value).some(v=>typeof v!=='string'))
    throw new Error('Invalid annotation label update');
  const internal=Object.hasOwn(config,'_wandb')?config._wandb:{};
  if (!mapping(internal)) throw new Error('Annotation metadata requires a mapping');
  const group=Object.hasOwn(internal,entry.type)?internal[entry.type]:{};
  if (!mapping(group)) throw new Error('Annotation layers require a mapping');
  config._wandb={...internal,[entry.type]:{...group,[entry.key]:entry}};
}
