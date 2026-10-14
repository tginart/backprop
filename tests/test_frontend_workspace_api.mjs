/**
 * Architecture: real auth/API modules -> deterministic Foundry HTTP fixtures ->
 * asserted identity, consent, immutable publishing and archive/live readback.
 * Main caller: node --test tests/test_frontend_workspace_api.mjs.
 * Fixtures use invented tokens; no user credentials or remote writes occur.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {makeWorkspaceEvent, replayWorkspaces, workspaceKey} from '../web/workspace-store.mjs';

const config = {host: 'https://foundry.example', client_id: 'workspace-test', app_url: 'https://backprop.example/',
  stream_dataset_rid: 'ri.foundry.main.dataset.events', media_dataset_rid: 'ri.foundry.main.dataset.media', stream_view_rid: 'view-1', branch: 'master'};
const scope = {entity: 'team', project: 'project', userId: 'alice', viewId: 'personal'};
const event = (id, parents = [], overrides = {}) => makeWorkspaceEvent({scope, eventId: id, parents, actorId: 'alice', name: 'Personal workspace',
  timestamp: '2026-10-07T12:00:00Z', snapshot: {version: 1, hiddenRuns: ['run-1'], layout: {sections: []}}, ...overrides});
const row = value => ({schema_version: value.schema_version, event_id: value.event_id, run_id: value.run_id, project: value.project,
  entity: value.entity, event_type: value.type, timestamp: value.timestamp, payload: JSON.stringify(value.payload)});
const csv = values => {
  const names = ['schema_version', 'event_id', 'run_id', 'project', 'entity', 'event_type', 'timestamp', 'payload'];
  return [names, ...values.map(value => names.map(name => value[name]))].map(cells => cells.map(cell => '"' + String(cell).replaceAll('"', '""') + '"').join(',')).join('\n');
};
let sequence = 0;
async function fixture({scopes = ['api:use-streams-write'], identity = {id: 'alice', username: 'Alice'}, respond} = {}) {
  const calls = [], memory = new Map([['backprop.oauth.session', JSON.stringify({accessToken: 'invented-fixture-token', expiresAt: Date.now() + 3600000, scopes})]]);
  globalThis.sessionStorage = {getItem: key => memory.get(key) ?? null, setItem: (key, value) => memory.set(key, value), removeItem: key => memory.delete(key)};
  globalThis.location = {href: config.app_url, origin: 'https://backprop.example', assign(value) {this.assigned = String(value);}};
  Object.defineProperty(globalThis, 'crypto', {value: webcrypto, configurable: true});
  globalThis.fetch = async (input, options = {}) => {
    if (input === './config.json') return Response.json(config);
    const url = new URL(input); calls.push({url, options});
    if (url.pathname === '/multipass/api/me') return identity instanceof Response ? identity : Response.json(identity);
    if (respond) return respond(url, options);
    throw new Error('Unexpected Foundry request: ' + url.pathname);
  };
  const api = await import(`../web/api.js?workspace-api-${++sequence}`);
  await api.initialize();
  return {api, calls, memory};
}

test('workspace identity uses real Multipass id and username under existing read authorization', async () => {
  const env = await fixture({scopes: [], identity: {id: 'alice', username: 'Alice', attributes: {private: ['not retained']}}});
  assert.deepEqual(await env.api.getWorkspaceIdentity(), {userId: 'alice', displayName: 'Alice'});
  assert.deepEqual(await env.api.getWorkspaceIdentity(), {userId: 'alice', displayName: 'Alice'});
  assert.equal(env.calls.length, 1); assert.equal(env.calls[0].url.pathname, '/multipass/api/me');
  assert.equal(env.calls[0].url.origin, config.host); assert.equal(env.calls[0].options.credentials, 'omit'); assert.equal(env.calls[0].options.redirect, 'error');
  assert.equal(env.calls[0].options.headers.Authorization, 'Bearer invented-fixture-token');
  assert.equal(env.calls.some(call => call.url.pathname.includes('admin')), false);
});

test('missing or denied user identity never becomes a synthetic personal workspace', async () => {
  for (const identity of [{username: 'Alice'}, {id: ''}, {id: 'alice', username: ''}, {id: 123, username: 'Alice'}]) {
    const env = await fixture({identity}); await assert.rejects(env.api.getWorkspaceIdentity(), /invalid workspace user identity/);
  }
  const denied = await fixture({identity: new Response(null, {status: 403})});
  await assert.rejects(denied.api.getWorkspaceIdentity(), error => error.status === 403 && /workspace identity/.test(error.message));
});

test('workspace publishing requires an existing write grant and does not start consent automatically', async () => {
  const env = await fixture({scopes: []});
  await assert.rejects(env.api.publishWorkspaceEvent(event('one')), /Enable workspace saving/);
  assert.equal(env.calls.length, 0); assert.equal(globalThis.location.assigned, undefined);
  const auth = await import('../web/auth.js');
  assert.equal(auth.getAuthState().canSaveWorkspaces, false);
  await auth.login({allowWorkspaceSave: true});
  const url = new URL(globalThis.location.assigned);
  assert(url.searchParams.get('scope').split(' ').includes('api:use-streams-write'));
  assert(!url.searchParams.get('scope').includes('admin'));
  assert.equal(env.calls.length, 0);
});

test('uncertain workspace save retries preserve byte-identical records and the stream view contract', async () => {
  let attempt = 0;
  const env = await fixture({respond: (url, options) => {
    assert.equal(url.pathname.split('/').at(-1), 'publishRecords');
    if (++attempt === 1) throw new TypeError('connection ended before acknowledgment');
    return new Response(null, {status: 204});
  }});
  const saved = event('fixed-revision');
  await assert.rejects(env.api.publishWorkspaceEvent(saved), /connection ended/);
  const result = await env.api.publishWorkspaceEvent(saved);
  const publishes = env.calls.filter(call => call.url.pathname.endsWith('/publishRecords'));
  assert.equal(publishes.length, 2); assert.equal(publishes[0].options.body, publishes[1].options.body);
  assert.deepEqual(result, saved);
  const body = JSON.parse(publishes[0].options.body);
  assert.deepEqual(body.records, [row(saved)]); assert.equal(body.viewRid, 'view-1');
  assert.equal(publishes[0].url.searchParams.get('preview'), 'true'); assert.equal(publishes[0].url.searchParams.has('viewRid'), false);
  assert.equal(publishes[0].options.redirect, 'error'); assert.equal(publishes[0].options.credentials, 'omit');
});

test('publisher validates actual editor identity, shared ownership, envelopes and revision collisions', async () => {
  const env = await fixture({respond: () => new Response(null, {status: 204})});
  await assert.rejects(env.api.publishWorkspaceEvent(event('wrong-user', [], {scope: {...scope, userId: 'bob', viewId: 'review'}, actorId: 'bob'})), /signed-in user/);
  assert.equal(env.calls.filter(call => call.url.pathname.endsWith('/publishRecords')).length, 0);
  const teammate = event('teammate', [], {scope: {...scope, userId: 'bob', viewId: 'review'}});
  assert.equal((await env.api.publishWorkspaceEvent(teammate)).payload.scope.userId, 'bob');
  await assert.rejects(env.api.publishWorkspaceEvent({...teammate, project: 'other'}), /crosses its scope/);
  await assert.rejects(env.api.publishWorkspaceEvent({...teammate, type: 'run_stop_request'}), /Only workspace snapshot/);
  await assert.rejects(env.api.publishWorkspaceEvent(event('teammate', [], {scope: teammate.payload.scope, snapshot: {different: true}})), /Conflicting workspace/);
  assert.equal(env.calls.filter(call => call.url.pathname.endsWith('/publishRecords')).length, 1);
});

test('workspace archive and live records merge, recover waking tabs and never hydrate display settings', async () => {
  const root = event('archive-root'), next = event('live-next', ['archive-root']), later = event('later-archive', ['live-next']);
  // The snapshot deliberately looks like an external rich value. Workspace
  // settings must not be interpreted as a request to fetch experiment media.
  next.payload.snapshot.preview = {external: true, _type: 'table', dataset_rid: config.media_dataset_rid, path: 'blobs/not-a-real-table'};
  let phase = 0, archiveQueries = 0;
  const env = await fixture({respond: url => {
    const operation = url.pathname.split('/').at(-1);
    if (operation === 'execute') {archiveQueries++; return Response.json({type: 'succeeded', queryId: 'archive'});}
    if (operation === 'getResults') return new Response(csv((phase ? [root, next, later] : [root]).map(row)));
    if (operation === 'getEndOffsets') return Response.json({'0': phase ? '2' : '1'});
    if (operation === 'getRecords') return Response.json([{offset: phase ? '1' : '0', value: row(next)}]);
    throw new Error('Workspace settings triggered unexpected media I/O: ' + url.pathname);
  }});
  const initial = await env.api.loadWorkspace();
  assert.deepEqual(initial.events.map(value => value.event_id).sort(), ['archive-root', 'live-next']);
  assert.equal(replayWorkspaces(initial.events).get(workspaceKey(scope)).revisionId, 'live-next');
  phase = 1;
  const recovered = await env.api.loadWorkspace({refreshArchive: true});
  assert.deepEqual(recovered.events.map(value => value.event_id).sort(), ['archive-root', 'later-archive', 'live-next']);
  assert.equal(replayWorkspaces(recovered.events).get(workspaceKey(scope)).revisionId, 'later-archive');
  assert.equal(archiveQueries, 2);
});

test('a workspace save rejected by Foundry stays a failed remote operation', async () => {
  const env = await fixture({respond: () => new Response(null, {status: 403})});
  await assert.rejects(env.api.publishWorkspaceEvent(event('denied')), error => error.status === 403 && /denied workspace saving/.test(error.message));
  assert.equal(env.calls.filter(call => call.url.pathname.endsWith('/publishRecords')).length, 1);
});
