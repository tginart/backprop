/**
 * Architecture: archive/live event fixtures -> real reducer and journal ->
 * asserted identity, acknowledgment, conflict and edit history behavior.
 * Main caller: node --test tests/test_frontend_workspace_store.mjs.
 * No browser profile, remote writes, credentials or package installation.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {WORKSPACE_EVENT_TYPE, workspaceKey, makeWorkspaceEvent, replayWorkspaces, createWorkspaceStore} from '../web/workspace-store.mjs';

const personal = {entity: 'team', project: 'project', userId: 'alice', viewId: 'personal'};
const shared = {...personal, viewId: 'review'};
const display = value => ({version: 1, visibleRuns: value, layout: {sections: []}});
function event(id, {scope = shared, parents = [], value = [id], actorId = scope.userId, timestamp = '2026-10-07T12:00:00Z', name = 'Review'} = {}) {
  return makeWorkspaceEvent({scope, snapshot: display(value), name, parents, actorId, eventId: id, timestamp});
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
function fixture({events = [], userId = 'alice', publish, load, journal, initialSequence = 0} = {}) {
  const remote = events.slice(), writes = [], changes = [], timers = new Map();
  let sequence = initialSequence, timerId = 0;
  const store = createWorkspaceStore({userId, debounceMs: 200, journal,
    newId: () => `save-${++sequence}`, clock: () => '2026-10-07T12:01:00Z',
    load: load || (async () => remote.slice()),
    publish: async value => {writes.push(value); if (publish) await publish(value); remote.push(value);},
    onChange: value => changes.push(value),
    schedule: callback => {timers.set(++timerId, callback); return timerId;}, cancel: id => timers.delete(id)});
  return {store, remote, writes, changes, timers,
    async fire() {const pending = [...timers.values()]; timers.clear(); pending.forEach(callback => callback()); await store.flush();}};
}

test('personal keys include user identity, shared keys use a project-wide view ID', () => {
  assert.notEqual(workspaceKey(personal), workspaceKey({...personal, userId: 'bob'}));
  assert.equal(workspaceKey(shared), workspaceKey({...shared, userId: 'bob'}));
  assert.notEqual(workspaceKey({...shared, entity: 'a/b', project: 'c'}), workspaceKey({...shared, entity: 'a', project: 'b/c'}));
  assert.notEqual(workspaceKey(shared), workspaceKey({...shared, project: 'other'}));
  assert.throws(() => workspaceKey({...shared, userId: ''}), /owner/);
});

function deviceJournal() {
  let envelope=null;
  return {read:()=>envelope===null ? null : structuredClone(envelope),write:value=>{envelope=structuredClone(value);}};
}

test('reload recovers a failed immutable save, newer draft and undo history before exact retry',async()=>{
  const journal=deviceJournal(), first=fixture({events:[event('root')],journal,publish:async()=>{throw new Error('Connection lost before acknowledgment.');}});
  await first.store.open(shared);first.store.edit(display(['first']));
  await assert.rejects(first.store.save(),/Connection lost/);
  first.store.edit(display(['newer']));const bytes=JSON.stringify(first.writes[0]);
  const saved=journal.read();assert.equal(saved.userId,'alice');assert.equal(saved.views[0].pending.event_id,'save-1');
  assert.deepEqual(saved.views[0].draft.visibleRuns,['newer']);assert.equal(Object.hasOwn(saved,'records'),false);
  first.store.dispose();
  const recovered=fixture({events:[event('root')],journal,initialSequence:1});
  await recovered.store.open(shared);
  assert.deepEqual(recovered.store.current().visibleRuns,['newer']);assert.equal(recovered.store.status().pending,true);
  assert.match(recovered.store.status().error,/Connection lost/);assert.equal(recovered.store.status().canUndo,true);
  assert.equal(recovered.writes.length,0);assert.equal(recovered.timers.size,0);
  await recovered.store.save();
  assert.equal(JSON.stringify(recovered.writes[0]),bytes);assert.deepEqual(recovered.writes[1].payload.parents,['save-1']);
  assert.deepEqual(recovered.writes[1].payload.snapshot.visibleRuns,['newer']);assert.equal(recovered.store.status().dirty,false);
  assert.equal(journal.read().views[0].pending,null);assert.equal(journal.read().views[0].error,'');
});

test('reload preserves a dirty draft when the real remote revision advanced and discard adopts it',async()=>{
  const journal=deviceJournal(),first=fixture({events:[event('root')],journal});
  await first.store.open(shared);first.store.edit(display(['device-draft']));first.store.dispose();
  const newer=event('remote-new',{parents:['root'],value:['teammate'],actorId:'bob'});
  const recovered=fixture({events:[event('root'),newer],journal});await recovered.store.open(shared);
  assert.deepEqual(recovered.store.current().visibleRuns,['device-draft']);assert.equal(recovered.store.status().conflict,true);
  await assert.rejects(recovered.store.save(),/changed elsewhere/);assert.equal(recovered.writes.length,0);
  assert.equal(recovered.store.discard(),true);assert.deepEqual(recovered.store.current().visibleRuns,['teammate']);
  assert.equal(recovered.store.status().conflict,false);assert.equal(recovered.store.status().dirty,false);
  assert.deepEqual(journal.read().views[0].baseHeads,['remote-new']);assert.equal(recovered.store.status().canUndo,false);
});

test('a real read acknowledges a previously accepted pending event without a duplicate publish',async()=>{
  const journal=deviceJournal(),first=fixture({events:[event('root')],journal,publish:async()=>{throw new Error('Response lost.');}});
  await first.store.open(shared);first.store.edit(display(['saved']));await assert.rejects(first.store.save());
  const accepted=first.writes[0];first.store.dispose();
  const recovered=fixture({events:[event('root'),accepted],journal});await recovered.store.open(shared);
  assert.equal(recovered.store.status().pending,false);assert.equal(recovered.store.status().dirty,false);
  assert.equal(recovered.store.status().revisionId,accepted.event_id);assert.equal(recovered.store.status().error,'');
  await recovered.store.flush();assert.equal(recovered.writes.length,0);
});

test('unreadable archive prevents recovered pending publishing and missing baselines stay explicit',async()=>{
  const journal=deviceJournal(),first=fixture({events:[event('root')],journal,publish:async()=>{throw new Error('Response lost.');}});
  await first.store.open(shared);first.store.edit(display(['pending']));await assert.rejects(first.store.save());first.store.dispose();
  const unavailable=fixture({journal,load:async()=>{throw new Error('Archive unavailable.');}});
  await assert.rejects(unavailable.store.open(shared),/Archive unavailable/);
  await assert.rejects(unavailable.store.save(),/Archive unavailable/);assert.equal(unavailable.writes.length,0);
  assert.equal(unavailable.store.status().loaded,false);assert.equal(unavailable.store.status().pending,true);
  const incomplete=fixture({journal});await incomplete.store.open(shared);
  assert.deepEqual(incomplete.store.status().missingParents,['root']);assert.equal(incomplete.store.status().conflict,true);
  assert.equal(incomplete.store.status().revisionId,null);
  await assert.rejects(incomplete.store.flush(),/history is incomplete/);assert.equal(incomplete.writes.length,0);
});

test('device journal failures are visible and block publishing before an exact pending event is recoverable',async()=>{
  let blocked=true,envelope=null;
  const journal={read:()=>null,write:value=>{if(blocked)throw new Error('Device quota exceeded.');envelope=structuredClone(value);}};
  const env=fixture({journal});await env.store.open(shared,{snapshot:display([])});env.store.edit(display(['draft']));
  assert.equal(env.store.status().journalEnabled,true);assert.match(env.store.status().journalError,/quota exceeded/);
  await assert.rejects(env.store.save(),/reload recovery/);assert.equal(env.writes.length,0);assert.equal(env.store.status().pending,true);
  blocked=false;await env.store.flush();assert.equal(env.writes.length,1);
  assert.equal(env.store.status().journalError,'');assert.equal(envelope.views[0].pending,null);
});

test('journal import rejects another identity, malformed envelopes and collisions before replacing a draft',async()=>{
  const env=fixture({events:[event('root')]});await env.store.open(shared);env.store.edit(display(['kept']));
  const valid=env.store.exportJournal();
  assert.throws(()=>env.store.restoreJournal({...valid,userId:'bob'}),/another user/);
  assert.throws(()=>env.store.restoreJournal({...valid,views:[...valid.views,...valid.views]}),/duplicate workspace/);
  const corrupted=structuredClone(valid);corrupted.views[0].pending=event('bad',{actorId:'bob'});
  assert.throws(()=>env.store.restoreJournal(corrupted),/another identity/);
  const injected=structuredClone(valid);injected.views[0].pending={...event('bad'),unexpected:'not canonical'};
  assert.throws(()=>env.store.restoreJournal(injected),/invalid or duplicate/);
  assert.deepEqual(env.store.current().visibleRuns,['kept']);
  const restored=fixture({events:[event('root')]});restored.store.restoreJournal(valid);await restored.store.open(shared);
  assert.deepEqual(restored.store.current().visibleRuns,['kept']);assert.equal(restored.store.status().canUndo,true);
});

test('reload can retain a draft or explicitly adopt fresh remote settings, while branch choice is explicit',async()=>{
  const env=fixture({events:[event('root')]});await env.store.open(shared);env.store.edit(display(['local']));
  env.remote.push(event('remote',{parents:['root'],value:['new']}));await env.store.reload();
  assert.deepEqual(env.store.current().visibleRuns,['local']);assert.equal(env.store.status().conflict,true);
  await env.store.reload({discardDraft:true});assert.deepEqual(env.store.current().visibleRuns,['new']);assert.equal(env.store.status().conflict,false);
  env.remote.push(event('branch',{parents:['root'],value:['branch'],actorId:'bob'}));await env.store.reload();
  assert.throws(()=>env.store.adoptRemote(),/Choose a remote revision/);
  env.store.adoptRemote({revisionId:'branch'});assert.deepEqual(env.store.current().visibleRuns,['branch']);assert.equal(env.store.status().conflict,true);
  await env.store.save({resolveConflict:true});assert.deepEqual(env.writes[0].payload.parents,['branch','remote']);
  assert.equal(env.store.status().conflict,false);
});

test('recovered personal drafts autosave after a verified read, while failed saves require an explicit retry',async()=>{
  const journal=deviceJournal(), first=fixture({journal});await first.store.open(personal,{snapshot:display([])});
  first.store.edit(display(['draft']));first.store.dispose();
  const recovered=fixture({journal});await recovered.store.open(personal);assert.equal(recovered.timers.size,1);
  await recovered.fire();assert.equal(recovered.writes.length,1);assert.equal(recovered.store.status().dirty,false);
  const failedJournal=deviceJournal(), failed=fixture({journal:failedJournal,publish:async()=>{throw new Error('Write denied.');}});
  await failed.store.open(personal,{snapshot:display([])});failed.store.edit(display(['failed']));await assert.rejects(failed.store.flush(),/denied/);failed.store.dispose();
  const retry=fixture({journal:failedJournal});await retry.store.open(personal);
  assert.equal(retry.timers.size,0);assert.match(retry.store.status().error,/denied/);
  await retry.store.open(shared,{snapshot:display([])});await retry.store.open(personal);
  assert.equal(retry.timers.size,0);assert.match(retry.store.status().error,/denied/);
  await retry.store.flush();assert.equal(retry.writes.length,1);assert.equal(JSON.stringify(retry.writes[0]),JSON.stringify(failed.writes[0]));
});

test('a failed Save-as remains an explicitly unsaved recoverable view with the same identity',async()=>{
  const journal=deviceJournal(), first=fixture({journal,publish:async()=>{throw new Error('Not acknowledged.');}});
  await first.store.open(shared,{snapshot:display(['seed']),name:'New review'});await assert.rejects(first.store.save());first.store.dispose();
  const recovered=fixture({journal}), drafts=recovered.store.recoverableViews('team','project');
  assert.equal(recovered.store.views('team','project').length,0);assert.equal(drafts.length,1);
  assert.equal(drafts[0].localDraft,true);assert.equal(drafts[0].revisionId,null);assert.equal(drafts[0].pending,true);
  assert.equal(drafts[0].name,'New review');assert.deepEqual(drafts[0].scope,shared);
  await recovered.store.open(drafts[0].scope);await recovered.store.flush();
  assert.equal(JSON.stringify(recovered.writes[0]),JSON.stringify(first.writes[0]));
  assert.equal(recovered.store.recoverableViews('team','project').length,0);assert.equal(recovered.store.views('team','project').length,1);
});

test('workspace envelopes fit the existing stream and exclude bulk selection and credentials', () => {
  const value = event('revision');
  assert.equal(value.type, WORKSPACE_EVENT_TYPE);
  assert.match(value.run_id, /^__workspace__:[0-9a-f]{64}$/);
  assert.notEqual(value.run_id, event('other', {scope: {...shared, project: 'elsewhere'}}).run_id);
  assert.equal(value.run_id, event('other', {actorId: 'bob'}).run_id);
  assert.throws(() => makeWorkspaceEvent({...value.payload, eventId: 'x', timestamp: value.timestamp, snapshot: {rowSelection: ['run']}}), /rowSelection/);
  assert.throws(() => makeWorkspaceEvent({...value.payload, eventId: 'x', timestamp: value.timestamp, snapshot: {accessToken: 'secret'}}), /accessToken/);
  assert.throws(() => event('x', {scope: personal, actorId: 'bob'}), /another user/);
  assert.throws(() => makeWorkspaceEvent({...value.payload, eventId: 'x', timestamp: value.timestamp, snapshot: {settings: new Map()}}), /JSON/);
  assert.throws(() => makeWorkspaceEvent({...value.payload, eventId: 'x', timestamp: value.timestamp, snapshot: {smoothing: NaN}}), /JSON/);
});

test('archive and live retries deduplicate, while conflicting revision identities fail', () => {
  const original = event('one'), next = event('two', {parents: ['one']});
  const record = {...original, event_type: original.type, schema_version: '1', payload: JSON.stringify(original.payload)};
  delete record.type;
  const replay = replayWorkspaces([next, record, original]);
  assert.equal(replay.get(workspaceKey(shared)).revisionId, 'two');
  assert.throws(() => replayWorkspaces([original, event('one', {value: ['different']})]), /Conflicting workspace/);
  assert.throws(() => replayWorkspaces([original, event('one', {scope: {...shared, project: 'elsewhere'}})]), /Conflicting workspace/);
  assert.throws(() => replayWorkspaces([{...original, project: 'elsewhere'}]), /crosses its scope/);
  assert.equal(replayWorkspaces([{type: 'run_start', payload: {}}]).size, 0);
});

test('parent revisions override clock skew and concurrent heads remain deterministic conflicts', () => {
  const root = event('root', {timestamp: '2026-10-07T14:00:00Z'});
  const next = event('next', {parents: ['root'], timestamp: '2026-10-07T10:00:00Z'});
  assert.equal(replayWorkspaces([next, root]).get(workspaceKey(shared)).revisionId, 'next');
  const other = event('other', {parents: ['root'], timestamp: next.timestamp, actorId: 'bob'});
  const first = replayWorkspaces([other, next, root]).get(workspaceKey(shared));
  const second = replayWorkspaces([root, next, other]).get(workspaceKey(shared));
  assert.deepEqual(first, second); assert.equal(first.conflict, true); assert.deepEqual(first.heads, ['next', 'other']);
  const resolution = event('resolved', {parents: first.heads});
  const resolved = replayWorkspaces([root, next, other, resolution]).get(workspaceKey(shared));
  assert.equal(resolved.conflict, false); assert.deepEqual(resolved.heads, ['resolved']);
});

test('missing, cross-view and cyclic parents are reported without pretending history is complete', () => {
  assert.deepEqual(replayWorkspaces([event('next', {parents: ['missing']})]).get(workspaceKey(shared)).missingParents, ['missing']);
  assert.throws(() => replayWorkspaces([event('other', {scope: {...shared, project: 'other'}}), event('next', {parents: ['other']})]), /different workspace/);
  assert.throws(() => replayWorkspaces([event('a', {parents: ['b']}), event('b', {parents: ['a']})]), /cycle/);
  assert.throws(() => event('self', {parents: ['self']}), /parent revisions/);
  assert.throws(() => replayWorkspaces([event('one'), event('two', {scope: {...shared, userId: 'bob'}, parents: ['one']})]), /original owner/);
});

test('personal edits debounce and become saved only after the publisher acknowledges them', async () => {
  const gate = deferred(), env = fixture({publish: () => gate.promise});
  await env.store.open(personal, {snapshot: display([])});
  env.store.edit(display(['a'])); env.store.edit(display(['b']));
  assert.equal(env.timers.size, 1); assert.equal(env.writes.length, 0); assert.equal(env.store.status().dirty, true);
  const saving = env.store.flush();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(env.writes.length, 1); assert.equal(env.store.status().pending, true); assert.equal(env.store.status().revisionId, null);
  gate.resolve(); await saving;
  assert.equal(env.store.status().dirty, false); assert.equal(env.store.status().saving, false); assert.equal(env.store.status().pending, false);
  assert.equal(env.store.status().revisionId, 'save-1'); assert.deepEqual(env.writes[0].payload.snapshot.visibleRuns, ['b']);
  assert.equal(env.timers.size, 0);
});

test('shared edits require Save and remain visible when opening another view and returning', async () => {
  const env = fixture({events: [event('root')]});
  await env.store.open(shared); env.store.edit(display(['draft']));
  assert.equal(env.timers.size, 0); await env.store.flush(); assert.equal(env.writes.length, 0);
  await env.store.open(personal, {snapshot: display([])}); await env.store.open(shared);
  assert.deepEqual(env.store.current().visibleRuns, ['draft']); assert.equal(env.store.status().canUndo, true);
  await env.store.save(); assert.equal(env.writes.length, 1); assert.deepEqual(env.writes[0].payload.parents, ['root']);
  assert.equal(env.store.status().dirty, false);
});

test('undo and redo are per view, personal undo autosaves and new edits clear redo', async () => {
  const env = fixture();
  await env.store.open(personal, {snapshot: display([])});
  env.store.edit(display(['one'])); env.store.edit(display(['two']));
  assert.equal(env.store.undo(), true); assert.deepEqual(env.store.current().visibleRuns, ['one']);
  await env.fire(); assert.deepEqual(env.writes.at(-1).payload.snapshot.visibleRuns, ['one']);
  assert.equal(env.store.redo(), true); assert.deepEqual(env.store.current().visibleRuns, ['two']);
  env.store.undo(); env.store.edit(display(['three'])); assert.equal(env.store.redo(), false);
  await env.store.open(shared, {snapshot: display([])}); assert.equal(env.store.undo(), false);
  await env.store.open(personal); assert.equal(env.store.status().canUndo, true);
  env.store.dispose(); assert.equal(env.timers.size, 0);
});

test('a denied save leaves a visible error and retry uses the identical immutable event', async () => {
  let attempt = 0;
  const env = fixture({publish: async () => {if (++attempt === 1) throw new Error('Foundry denied stream writing.');}});
  await env.store.open(shared, {snapshot: display([])}); env.store.edit(display(['a']));
  await assert.rejects(env.store.save(), /denied/);
  assert.equal(env.store.status().dirty, true); assert.equal(env.store.status().pending, true);
  assert.match(env.store.status().error, /denied/); assert.equal(env.store.status().revisionId, null);
  await env.store.flush(); assert.deepEqual(env.writes[0], env.writes[1]); assert.equal(env.store.status().error, '');
});

test('edits during a personal publish create a successor, never a false saved status', async () => {
  const gate = deferred(); let count = 0;
  const env = fixture({publish: async () => {if (++count === 1) await gate.promise;}});
  await env.store.open(personal, {snapshot: display([])}); env.store.edit(display(['first']));
  const saving = env.store.flush(); await new Promise(resolve => setImmediate(resolve));
  env.store.edit(display(['second'])); gate.resolve(); await saving;
  assert.equal(env.writes.length, 2); assert.deepEqual(env.writes[1].payload.parents, ['save-1']);
  assert.deepEqual(env.store.current().visibleRuns, ['second']); assert.equal(env.store.status().dirty, false);
});

test('edits during a shared publish stay unsaved until another explicit Save', async () => {
  const gate = deferred(), env = fixture({publish: () => gate.promise});
  await env.store.open(shared, {snapshot: display([])}); env.store.edit(display(['first']));
  const saving = env.store.save(); await new Promise(resolve => setImmediate(resolve));
  env.store.edit(display(['second'])); gate.resolve(); await saving;
  assert.equal(env.writes.length, 1); assert.equal(env.store.status().dirty, true);
  assert.deepEqual(env.store.current().visibleRuns, ['second']);
});

test('a fresh remote revision blocks stale saves; explicit resolution uses the latest remote parent', async () => {
  const env = fixture({events: [event('root')]});
  await env.store.open(shared); env.store.edit(display(['local']));
  env.remote.push(event('teammate', {parents: ['root'], actorId: 'bob'}));
  await assert.rejects(env.store.save(), /changed elsewhere/); assert.equal(env.writes.length, 0);
  assert.equal(env.store.status().conflict, true); assert.deepEqual(env.store.current().visibleRuns, ['local']);
  await env.store.save({resolveConflict: true}); assert.deepEqual(env.writes[0].payload.parents, ['teammate']);
  assert.equal(env.store.status().conflict, false); assert.equal(env.store.status().dirty, false);
});

test('an explicit save after a failed attempt first retries the original, then saves the newer draft and name', async () => {
  let attempt = 0;
  const env = fixture({publish: async () => {if (++attempt === 1) throw new Error('No acknowledgment.');}});
  await env.store.open(shared, {snapshot: display([])}); env.store.edit(display(['first']));
  await assert.rejects(env.store.save(), /acknowledgment/);
  env.store.edit(display(['newer'])); await env.store.save({name: 'Renamed review'});
  assert.equal(env.writes.length, 3); assert.deepEqual(env.writes[0], env.writes[1]);
  assert.deepEqual(env.writes[2].payload.parents, ['save-1']);
  assert.deepEqual(env.writes[2].payload.snapshot.visibleRuns, ['newer']);
  assert.equal(env.writes[2].payload.name, 'Renamed review'); assert.equal(env.store.status().dirty, false);
});

test('a save racing with another editor retains the acknowledged local draft and exposes both revisions', async () => {
  const gate = deferred(), env = fixture({events: [event('root')], publish: () => gate.promise});
  await env.store.open(shared); env.store.edit(display(['local']));
  const saving = env.store.save(); await new Promise(resolve => setImmediate(resolve));
  env.store.ingest([event('teammate', {parents: ['root'], actorId: 'bob', timestamp: '2026-10-07T15:00:00Z'})]);
  gate.resolve(); await assert.rejects(saving, /conflicts with this version/);
  assert.equal(env.store.status().conflict, true); assert.deepEqual(env.store.current().visibleRuns, ['local']);
  assert.deepEqual(env.store.status().conflictRevisions, ['save-1', 'teammate']);
});

test('shared views can be saved by teammates while personal views stay identity-scoped', async () => {
  const env = fixture({userId: 'bob', events: [event('root'), event('alice-personal', {scope: personal})]});
  await env.store.open(shared); env.store.edit(display(['bob-edit'])); await env.store.save();
  assert.equal(env.writes[0].payload.scope.userId, 'alice'); assert.equal(env.writes[0].payload.actorId, 'bob');
  assert.deepEqual(env.store.views('team', 'project').map(view => view.scope.viewId), ['review']);
  await assert.rejects(env.store.open(personal), /another user/);
});

test('reload recovery loads archived settings and load failure never publishes a replacement', async () => {
  const env = fixture({events: [event('old-archive', {scope: personal, value: ['recovered']})]});
  await env.store.open(personal); assert.deepEqual(env.store.current().visibleRuns, ['recovered']);
  assert.equal(env.writes.length, 0); assert.equal(env.store.status().canUndo, false);
  const failed = fixture({load: async () => {throw new Error('Archive unavailable.');}});
  await assert.rejects(failed.store.open(personal), /Archive unavailable/);
  failed.store.edit(display(['draft'])); assert.equal(failed.timers.size, 0);
  await assert.rejects(failed.store.save(), /Archive unavailable/); assert.equal(failed.writes.length, 0);
  assert.match(failed.store.status().error, /Archive unavailable/);
});

test('clean views adopt remote saves while dirty views retain their own draft', async () => {
  const env = fixture({events: [event('root')]});
  await env.store.open(shared);
  env.store.ingest([event('new', {parents: ['root'], value: ['remote']})]);
  assert.deepEqual(env.store.current().visibleRuns, ['remote']);
  env.store.edit(display(['local']));
  env.store.ingest([event('newer', {parents: ['new'], value: ['remote-newer']})]);
  assert.deepEqual(env.store.current().visibleRuns, ['local']); assert.equal(env.store.status().conflict, true);
  assert.throws(() => env.store.ingest([event('newer', {parents: ['new'], value: ['different']})]), /Conflicting workspace/);
  assert.deepEqual(env.store.current().visibleRuns, ['local']);
});
