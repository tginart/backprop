/**
 * Architecture: Foundry OAuth -> durable SQL archive plus incremental stream
 * reads -> de-duplicated canonical SDK events -> the visualization model.
 * Main callers: app.initialize and the 30-second live refresh loop.
 * Why this exists: native Foundry storage serves both old and fresh experiments
 * without a custom server, database, scheduled transform, or Ontology object.
 */
import {withDeadline, bufferedResponse} from './network.mjs';
import {initAuth, getAccessToken, getAuthState} from './auth.js';
import {isWorkspaceEvent, makeWorkspaceEvent, replayWorkspaces} from './workspace-store.mjs';
export {isWorkspaceEvent} from './workspace-store.mjs';

let config;
let workspaceIdentity = null;
let archiveLoaded = false;
let livePageSize = 100;
const events = new Map();
const offsets = new Map();
const mediaUrls = new Map();
const richValues = new Map();
const MAX_EVENTS = 1_000_000;
const RESOURCE_ID = /^ri\.foundry\.[a-zA-Z0-9-]+\.dataset\.[a-zA-Z0-9-]+$/;

export async function initialize() {
  config = await withDeadline(async signal => {
    const response = await fetch('./config.json', {cache:'no-store', signal});
    if (!response.ok) throw new Error('Backprop deployment configuration could not be loaded.');
    return response.json();
  });
  const origin = new URL(config.host);
  if (origin.protocol !== 'https:' || origin.origin !== config.host || !config.client_id ||
      !RESOURCE_ID.test(config.stream_dataset_rid) || !RESOURCE_ID.test(config.media_dataset_rid)) {
    throw new Error('Backprop deployment configuration is invalid.');
  }
  await initAuth(config);
  workspaceIdentity = null;
  return config;
}

export function getConfiguration() { return config ? {...config} : null; }

async function request(path, {body, signal: outerSignal, timeoutMs, raw = false, method, controlWrite = false, workspaceWrite = false} = {}) {
  return withDeadline(async signal => {
  if (!config) throw new Error('Backprop has not initialized.');
  if (!(path.startsWith('/api/') || path === '/multipass/api/me') || path.includes('://')) throw new Error('Invalid Foundry API path.');
  const token = await getAccessToken();
  if (controlWrite && !getAuthState().canControlRuns) throw new Error('Enable run controls to grant Foundry stream-write permission.');
  if (workspaceWrite && !getAuthState().canSaveWorkspaces) throw new Error('Enable workspace saving to grant Foundry stream-write permission.');
  const response = await fetch(config.host + path, {
    method: method || (body ? 'POST' : 'GET'), signal, credentials: 'omit', redirect: 'error',
    headers: {Authorization: `Bearer ${token}`, ...(body ? {'Content-Type': 'application/json'} : {})},
    ...(body ? {body: JSON.stringify(body)} : {}),
  });
  if (!response.ok) {
    const message = response.status === 401 ? 'Your Foundry session expired. Sign in again.' :
      response.status === 403 ? workspaceWrite ? 'Foundry denied workspace saving. This account needs write access to the Backprop event stream.' : controlWrite ? 'Foundry denied the stop request. This account needs write access to the Backprop event stream.' : path === '/multipass/api/me' ? 'Foundry denied access to your workspace identity. Sign in again.' : 'Foundry denied access. This account needs read access to the Backprop project.' :
      `Foundry request failed (${response.status}). Retry when the service is available.`;
    throw Object.assign(new Error(message), {status: response.status});
  }
  if (raw) return bufferedResponse(response);
  if (response.status === 204) return null;
  try { return await response.json(); }
  catch (error) {
    // Only failures while consuming a successful JSON body can trigger smaller
    // stream pages. Denials, connection setup failures and cancellation propagate.
    if (!signal?.aborted && (error instanceof TypeError || error instanceof SyntaxError)) error.responseTruncated = true;
    throw error;
  }
  }, {signal:outerSignal, timeoutMs});
}

/** Resolve a real current-user identity within the existing restricted app.
 * Main callers: workspace boot and the workspace publisher. Multipass me is an
 * identification endpoint explicitly exempted from restricted-app API scoping;
 * it requires no administrator grant. Missing identity fails visibly instead
 * of using a browser-generated ID or treating all users as one personal view.
 * Only id and username are retained; profile attributes never enter snapshots.
 */
export async function getWorkspaceIdentity({signal} = {}) {
  await getAccessToken();
  if (!workspaceIdentity) {
    const user = await request('/multipass/api/me', {signal});
    if (typeof user?.id !== 'string' || !user.id.trim() || user.id.length > 512 ||
        typeof user.username !== 'string' || !user.username.trim()) throw new Error('Foundry returned an invalid workspace user identity.');
    workspaceIdentity = {userId: user.id, displayName: user.username};
  }
  return {...workspaceIdentity};
}

/** Publish one display snapshot through the existing authorized event stream.
 * Main callers: workspace-store's injected publisher. The event keeps its ID,
 * parents, timestamp and snapshot across uncertain retries. Authentication fixes
 * the actor identity, while shared views retain their original owner metadata.
 * The API never requests a new grant, uploads files, or reports local-only save
 * success. Later reads merge this acknowledged event with archive/live copies.
 */
export async function publishWorkspaceEvent(event, {signal} = {}) {
  if (!isWorkspaceEvent(event) || event.type !== 'workspace_snapshot') throw new Error('Only workspace snapshot events may use the workspace publisher.');
  replayWorkspaces([event]);
  const snapshot = makeWorkspaceEvent({...event.payload, eventId: event.event_id, timestamp: event.timestamp});
  await getAccessToken();
  if (!getAuthState().canSaveWorkspaces) throw new Error('Enable workspace saving to grant Foundry stream-write permission.');
  const identity = await getWorkspaceIdentity({signal});
  if (snapshot.payload.actorId !== identity.userId) throw new Error('The workspace save editor does not match the signed-in user.');
  checkWorkspaceRetry(snapshot);
  const record = {schema_version: 1, event_id: snapshot.event_id, run_id: snapshot.run_id,
    project: snapshot.project, entity: snapshot.entity, event_type: snapshot.type, timestamp: snapshot.timestamp,
    payload: JSON.stringify(snapshot.payload)};
  const body = {records: [record], ...(config.stream_view_rid ? {viewRid: config.stream_view_rid} : {})};
  await request(streamPath('publishRecords'), {body, signal, workspaceWrite: true, raw: true});
  remember(record);
  return recordToEvent(record);
}

/** Parse CSV without losing embedded commas, doubled quotes, or line breaks.
 * Main callers: loadArchive, exported for contract tests.
 */
export function parseCsv(text) {
  text = text.replace(/^\uFEFF/, '');
  const rows = [];
  let row = [], field = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"' && field === '') quoted = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(field); rows.push(row); row = []; field = '';
    } else field += c;
  }
  if (quoted) throw new Error('Foundry returned incomplete CSV history.');
  if (field || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return [];
  const headers = rows.shift();
  return rows.filter(values => values.length !== 1 || values[0] !== '').map(values => {
    if (values.length !== headers.length) throw new Error('Foundry returned malformed CSV history.');
    return Object.fromEntries(headers.map((name, i) => [name, values[i]]));
  });
}

export function recordToEvent(row) {
  if (Number(row.schema_version) !== 1 || !row.event_id || !row.run_id || !row.event_type) {
    throw new Error('Foundry contains an unsupported Backprop event.');
  }
  return {schema_version: 1, event_id: row.event_id, run_id: row.run_id, project: row.project,
    entity: row.entity || '', type: row.event_type, timestamp: row.timestamp,
    payload: typeof row.payload === 'string' ? JSON.parse(row.payload) : row.payload};
}

function remember(row) {
  const event = recordToEvent(row);
  checkWorkspaceRetry(event);
  events.set(event.event_id, event);
  if (events.size >= MAX_EVENTS) throw new Error('This workspace exceeds one million events. Use a separate Backprop installation for additional history.');
}

/** Reject workspace revision collisions before caching or publishing a retry.
 * Main callers: remember and publishWorkspaceEvent. Rich run records retain
 * their existing hydration behavior; workspace payloads are never hydrated.
 */
function checkWorkspaceRetry(event) {
  const previous = events.get(event.event_id);
  if (!previous || !isWorkspaceEvent(event) && !isWorkspaceEvent(previous)) return;
  if (!isWorkspaceEvent(event) || !isWorkspaceEvent(previous)) throw new Error('A workspace revision ID collides with an experiment event.');
  replayWorkspaces([previous, event]);
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(signal.reason); return; }
    const abort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, ms);
    signal?.addEventListener('abort', abort, {once: true});
  });
}

async function loadArchive(outerSignal) {
  let status, path;
  try {
  await withDeadline(async signal => {
  status = await request('/api/v2/sqlQueries/execute', {signal, body: {
    query: `SELECT * FROM \`${config.stream_dataset_rid}\` ORDER BY timestamp LIMIT ${MAX_EVENTS}`,
    serializationFormat: 'CSV', fallbackBranchIds: [config.branch || 'master'],
  }});
  path = `/api/v2/sqlQueries/${encodeURIComponent(status.queryId)}`;
  const deadline = Date.now() + 180000;
    while (status.type === 'running') {
      if (Date.now() > deadline) throw new Error('Foundry history query timed out. Please retry.');
      await sleep(1000, signal);
      status = await request(`${path}/getStatus`, {signal});
    }
    if (status.type !== 'succeeded') throw new Error(`Foundry history query ${status.type}. Please retry.`);
    const result = await request(`${path}/getResults`, {raw: true, signal});
    const rows = parseCsv(await result.text());
    if (rows.length >= MAX_EVENTS) throw new Error('The history query reached its one-million-event limit.');
    rows.forEach(remember);
    archiveLoaded = true;
  }, {signal:outerSignal, timeoutMs:180000});
  } catch (error) {
    if (path && status?.type === 'running') {
      try { await request(`${path}/cancel`, {method:'POST', timeoutMs:5000}); }
      catch (cancelError) { console.warn('Could not cancel the history query:', cancelError.message); }
    }
    throw error;
  }
}

function streamPath(operation, extra = {}) {
  const query = new URLSearchParams({preview: 'true', ...extra});
  if (config.stream_view_rid && operation !== 'publishRecords') query.set('viewRid', config.stream_view_rid);
  return `/api/v2/highScale/streams/datasets/${encodeURIComponent(config.stream_dataset_rid)}/streams/${encodeURIComponent(config.branch || 'master')}/${operation}?${query}`;
}

/** Return one run's lifecycle/control envelopes from shared workspace reads.
 * Main callers: the stop dialog's fresh-state check and acknowledgment polling.
 * The same stream/archive cache backs ordinary workspace reads. No separate
 * backend or guessed process identity is introduced by this control surface.
 * Reads still scan available stream records before filtering; they skip media I/O.
 */
export async function loadRunControl(target, {signal, refreshArchive = false} = {}) {
  if (!archiveLoaded || refreshArchive) { await loadArchive(signal); offsets.clear(); }
  await loadLive(signal);
  const types = new Set(['run_start', 'run_finish', 'finish', 'run_stop_request', 'run_stop_ack']);
  return {events: [...events.values()].filter(event => event.run_id === target.id && event.project === target.project &&
    (event.entity || '') === (target.entity || '') && types.has(event.type)), fetchedAt: new Date().toISOString()};
}

/** Publish exactly one stop request for a previously displayed process instance.
 * Main callers: explicit confirmation in the stop dialog. The caller retains
 * requestId and timestamp through uncertain network retries; no automatic retry
 * invents another operation or targets a newly resumed process.
 */
export async function publishRunStop({id, project, entity = '', instanceId, requestId, timestamp}, {signal} = {}) {
  if (![id, project, instanceId, requestId, timestamp].every(value => typeof value === 'string' && value.length > 0) ||
      typeof entity !== 'string' || !Number.isFinite(Date.parse(timestamp))) throw new Error('The stop request is incomplete.');
  const record = {schema_version: 1, event_id: requestId, run_id: id, project, entity, event_type: 'run_stop_request', timestamp,
    payload: JSON.stringify({action: 'stop', instance_id: instanceId})};
  const body = {records: [record], ...(config.stream_view_rid ? {viewRid: config.stream_view_rid} : {})};
  await request(streamPath('publishRecords'), {body, signal, controlWrite: true, raw: true});
  return recordToEvent(record);
}

async function loadLive(signal) {
  const ends = await request(streamPath('getEndOffsets'), {signal});
  for (const [partition, end] of Object.entries(ends)) {
    let position = offsets.get(partition) || 0n;
    const boundary = BigInt(end);
    while (position < boundary) {
      const start = offsets.has(partition) ? {startOffset: String(position)} : {};
      let rows;
      try {
        rows = await request(streamPath('getRecords', {partitionId: partition, ...start, limit: String(livePageSize)}), {signal});
      } catch (error) {
        // Retry the same snapshot position, never consume partial JSON or advance
        // an offset on failure. A broken single record still produces an error.
        if (!error.responseTruncated || signal?.aborted || livePageSize === 1) throw error;
        livePageSize = Math.max(1, Math.floor(livePageSize / 2));
        continue;
      }
      if (!rows.length) break;
      let next = position;
      for (const row of rows) {
        const offset = BigInt(row.offset);
        if (offset >= boundary) break;
        remember(row.value);
        if (offset >= next) next = offset + 1n;
      }
      if (next <= position) throw new Error('Foundry returned non-advancing stream offsets. Refresh the page to retry.');
      position = next;
      offsets.set(partition, position);
    }
  }
}

/** Merge durable history, then read only new events on each automatic refresh.
 * Main callers: app refresh loop; wake/manual refresh requests archive recovery.
 * Why this exists: a suspended tab can outlive hot-stream retention. Re-reading
 * the archive and currently available stream closes that gap without assuming
 * that Foundry rejects old offsets. Event IDs remove the overlap.
 * Errors leave existing charts visible and are
 * shown by the UI; no mock data is substituted for an unavailable connection.
 */
export async function loadWorkspace({project, signal, refreshArchive = false} = {}) {
  if (!archiveLoaded || refreshArchive) {
    await loadArchive(signal);
    offsets.clear();
  }
  await loadLive(signal);
  const selected = [...events.values()].filter(event => !project || event.project === project);
  // Bound concurrent large-table downloads while sharing content-addressed blobs.
  let cursor = 0;
  await Promise.all(Array.from({length: Math.min(4, selected.length)}, async () => {
    while (cursor < selected.length) {
      const event = selected[cursor++];
      if (!isWorkspaceEvent(event)) event.payload = await hydrate(event.payload, signal);
    }
  }));
  return {events: selected.sort((a, b) => a.timestamp.localeCompare(b.timestamp)),
  fetchedAt: new Date().toISOString(), source: 'Foundry stream + durable archive'};
}

async function hydrate(value, signal) {
  if (Array.isArray(value)) return Promise.all(value.map(item => hydrate(item, signal)));
  if (!value || typeof value !== 'object') return value;
  if (value.external === true && value._type) {
    const key = `${value.dataset_rid}/${value.path}`;
    if (!richValues.has(key)) {
      const pending = readMedia(value, signal).then(response => response.json())
        .then(data => {
          if (data?._type !== value._type) throw new Error('A logged rich-data file has the wrong type.');
          return data;
        }).catch(error => { richValues.delete(key); throw error; });
      richValues.set(key, pending);
    }
    return hydrate(await richValues.get(key), signal);
  }
  const result = Object.create(null);
  for (const [key, item] of Object.entries(value)) result[key] = await hydrate(item, signal);
  return result;
}

function readMedia(descriptor, signal) {
  if (descriptor.dataset_rid !== config.media_dataset_rid || typeof descriptor.path !== 'string' ||
      !descriptor.path.startsWith('blobs/')) throw new Error('Invalid Backprop media reference.');
  const query = new URLSearchParams({branchName: descriptor.branch || config.branch || 'master'});
  return request(`/api/v2/datasets/${encodeURIComponent(descriptor.dataset_rid)}/files/${encodeURIComponent(descriptor.path)}/content?${query}`, {raw: true, signal});
}

/** Fetch a logged file using the user's token, then expose a local blob URL.
 * Main callers: tables/media/artifact panels. Restrict files to this deployment's
 * media dataset so logged data cannot direct credentials to another location.
 */
export async function getMediaUrl(value) {
  if ((value?.file || value)?._backprop_external_reference) throw new Error("External artifact bytes require the Python SDK and provider credentials; they are not stored in Foundry.");
  const descriptor = value.file || value;
  const key = `${descriptor.dataset_rid}/${descriptor.path}`;
  if (mediaUrls.has(key)) return mediaUrls.get(key);
  const response = await readMedia(descriptor);
  const blob = await response.blob();
  const typed = new Blob([blob], {type: descriptor.mime_type || 'application/octet-stream'});
  const url = URL.createObjectURL(typed);
  mediaUrls.set(key, url);
  return url;
}
