/**
 * Architecture: display-only snapshots -> per-view undo journal -> immutable
 * Foundry event envelopes -> archive/live replay -> acknowledged saved state.
 * Main callers: the workspace UI, through injected load/publish functions.
 * Why this exists: personal edits autosave, while project-shared named views
 * require Save. A browser draft or a failed HTTP request never means Saved.
 *
 * Invariants: personal identities include the authenticated user; shared view
 * identities do not. Parent revisions detect stale and concurrent saves without
 * trusting browser clocks. Experiment readers must exclude WORKSPACE_EVENT_TYPE
 * before reconstructing runs. The transport must load archive plus live events;
 * hot-stream retention alone cannot provide durable reload recovery. An optional
 * device journal retains drafts and exact pending events across reloads; it is
 * recovery data, never evidence that a Foundry save succeeded.
 */
import {sha256} from './sha256.mjs';

export const WORKSPACE_EVENT_TYPE = 'workspace_snapshot';
export const PERSONAL_VIEW_ID = 'personal';
export const isWorkspaceEvent = event => (event?.type || event?.event_type) === WORKSPACE_EVENT_TYPE;
const forbidden = new Set(['rowSelection', 'selectedRows', 'checkedRuns', 'selected', 'runs', 'events', 'model', 'auth', 'accessToken', 'refreshToken']);
const compareText = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const compareEvents = (a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp) || compareText(a.event_id, b.event_id);
const requiredText = (value, label) => {
  if (typeof value !== 'string' || !value.trim() || value.length > 512) throw new Error(`Workspace ${label} is invalid.`);
  return value;
};
function copyJson(value) {
  function check(item) {
    if (item === null || ['string', 'boolean'].includes(typeof item)) return;
    if (typeof item === 'number' && Number.isFinite(item)) return;
    if (Array.isArray(item)) { Array.from(item).forEach(check); return; }
    if (item && typeof item === 'object' && [Object.prototype, null].includes(Object.getPrototypeOf(item))) {
      Object.values(item).forEach(check); return;
    }
    throw new Error('Workspace snapshots must contain finite JSON values only.');
  }
  // JSON serialization also detects cycles instead of silently dropping fields.
  let serialized;
  try { serialized = JSON.stringify(value); }
  catch { throw new Error('Workspace snapshots must contain finite JSON values only.'); }
  if (serialized === undefined) throw new Error('The workspace snapshot is missing.');
  check(value);
  return JSON.parse(serialized);
}
function snapshotJson(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('The workspace display snapshot is invalid.');
  for (const key of Object.keys(value)) if (forbidden.has(key)) throw new Error(`Workspace display snapshots cannot contain ${key}.`);
  return copyJson(value);
}
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}
const same = (left, right) => canonical(left) === canonical(right);
function checkedScope(scope) {
  if (!scope || typeof scope !== 'object' || typeof scope.entity !== 'string') throw new Error('Workspace scope is invalid.');
  return {entity: scope.entity, project: requiredText(scope.project, 'project'),
    userId: requiredText(scope.userId, 'owner'), viewId: requiredText(scope.viewId, 'view ID')};
}
function checkedName(name) {
  if (typeof name !== 'string' || !name.trim() || name.trim().length > 80) throw new Error('Use a workspace name between 1 and 80 characters.');
  return name.trim();
}

/** Build an unambiguous identity, independent of the editor for shared views.
 * Main callers: event creation, replay and journal view switching.
 */
export function workspaceKey(scope) {
  const value = checkedScope(scope);
  return JSON.stringify([value.entity, value.project, value.viewId === PERSONAL_VIEW_ID ? 'personal' : 'shared',
    value.viewId === PERSONAL_VIEW_ID ? value.userId : value.viewId]);
}
const workspaceRunId = scope => '__workspace__:' + sha256(new TextEncoder().encode(workspaceKey(scope)));

/** Create one immutable, retryable save using the existing stream schema.
 * Main callers: the journal save loop and transport contract tests. userId in
 * scope is the original owner; actorId is supplied from authenticated identity.
 * A shared view keeps its original owner when a teammate saves its next version.
 */
export function makeWorkspaceEvent({scope, snapshot, name, parents = [], actorId, eventId, timestamp}) {
  const identity = checkedScope(scope), id = requiredText(eventId, 'revision ID');
  if (typeof timestamp !== 'string' || !Number.isFinite(Date.parse(timestamp))) throw new Error('Workspace save timestamp is invalid.');
  if (!Array.isArray(parents) || parents.length > 64 || new Set(parents).size !== parents.length || parents.includes(id)) throw new Error('Workspace parent revisions are invalid.');
  parents.forEach(value => requiredText(value, 'parent revision'));
  const actor = requiredText(actorId, 'editor');
  if (identity.viewId === PERSONAL_VIEW_ID && actor !== identity.userId) throw new Error('A personal workspace belongs to another user.');
  const event = {schema_version: 1, event_id: id, run_id: workspaceRunId(identity), project: identity.project,
    entity: identity.entity, type: WORKSPACE_EVENT_TYPE, timestamp,
    payload: {version: 1, scope: identity, actorId: actor, name: checkedName(name), parents: parents.slice().sort(compareText), snapshot: snapshotJson(snapshot)}};
  if (new TextEncoder().encode(JSON.stringify(event)).length > 800_000) throw new Error('Workspace settings exceed the stream save limit.');
  return event;
}

/** Replay complete snapshots without allowing retries or clocks to lose edits.
 * Main callers: the store's ingest and independent archive/reload tests.
 * Why this exists: a later timestamp is not proof that a save knew about a
 * teammate's edit. Multiple unreferenced revisions remain visible as conflicts.
 * Missing parents are reported explicitly so incomplete archive reads cannot
 * masquerade as a verified saved workspace.
 */
export function replayWorkspaces(input) {
  if (!Array.isArray(input)) throw new Error('Workspace load must return an event array.');
  const records = new Map(), groups = new Map();
  for (const raw of input) {
    if ((raw?.type || raw?.event_type) !== WORKSPACE_EVENT_TYPE) continue;
    let payload;
    try { payload = typeof raw.payload === 'string' ? JSON.parse(raw.payload) : raw.payload; }
    catch { throw new Error('A saved workspace record cannot be decoded.'); }
    if (Number(raw.schema_version) !== 1 || payload?.version !== 1) throw new Error('A saved workspace uses an unsupported format.');
    const event = makeWorkspaceEvent({...payload, eventId: raw.event_id, timestamp: raw.timestamp});
    if (raw.run_id !== event.run_id || raw.project !== event.project || (raw.entity || '') !== event.entity) throw new Error('A saved workspace envelope crosses its scope.');
    const previous = records.get(event.event_id);
    if (previous && !same(previous, event)) throw new Error(`Conflicting workspace records for revision ${event.event_id}.`);
    records.set(event.event_id, event);
  }
  for (const event of records.values()) {
    const key = workspaceKey(event.payload.scope);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(event);
  }
  const result = new Map();
  for (const [key, events] of groups) {
    const referenced = new Set(), missing = new Set(), visiting = new Set(), visited = new Set();
    const owner = events[0].payload.scope.userId;
    function visit(event) {
      if (visiting.has(event.event_id)) throw new Error('Saved workspace revisions contain a cycle.');
      if (visited.has(event.event_id)) return;
      visiting.add(event.event_id);
      for (const parentId of event.payload.parents) {
        referenced.add(parentId);
        const parent = records.get(parentId);
        if (!parent) { missing.add(parentId); continue; }
        if (workspaceKey(parent.payload.scope) !== key) throw new Error('A workspace revision references a different workspace.');
        visit(parent);
      }
      visiting.delete(event.event_id); visited.add(event.event_id);
    }
    for (const event of events) {
      if (event.payload.scope.userId !== owner) throw new Error('A shared workspace changed its original owner.');
      visit(event);
    }
    const heads = events.filter(event => !referenced.has(event.event_id)).sort(compareEvents);
    const latest = heads.at(-1);
    result.set(key, {key, scope: copyJson(latest.payload.scope), name: latest.payload.name,
      snapshot: copyJson(latest.payload.snapshot), revisionId: latest.event_id, updatedAt: latest.timestamp,
      heads: heads.map(event => event.event_id), conflict: heads.length > 1, missingParents: [...missing].sort(compareText)});
  }
  return result;
}

/** Own per-view edits and saved acknowledgments without DOM or browser storage.
 * Main callers: app boot/view selection, display edits, Save and Undo/Redo.
 * load(scope) must return all relevant archive+live canonical events. publish
 * resolves only after Foundry acknowledges that exact event. The optional
 * synchronous journal.read()/write(envelope) is injected by the app, keeping
 * browser storage out of the reducer. Pending events are written before publish.
 * onChange receives {snapshot,status} for the currently open view. The journal
 * contains display snapshots and undo history, never tokens or experiment data.
 */
export function createWorkspaceStore({userId, load, publish, onChange = () => {}, debounceMs = 600,
  clock = () => new Date().toISOString(), newId = () => crypto.randomUUID(),
  schedule = setTimeout, cancel = clearTimeout, historyLimit = 50, journal}) {
  const actorId = requiredText(userId, 'authenticated user');
  if (typeof load !== 'function' || typeof publish !== 'function' || !Number.isFinite(debounceMs) || debounceMs < 0 || !Number.isInteger(historyLimit) || historyLimit < 1) throw new Error('Workspace store configuration is invalid.');
  if(journal && (typeof journal.read!=='function' || typeof journal.write!=='function'))throw new Error('Workspace recovery journal needs synchronous read and write callbacks.');
  const sessions = new Map(), records = new Map();
  let catalog = new Map(), activeKey = null, journalError = '';
  const active = () => {
    const entry = sessions.get(activeKey);
    if (!entry) throw new Error('Open a workspace before editing it.');
    return entry;
  };
  const remote = entry => catalog.get(entry.key);
  const dirty = entry => !same(entry.draft, entry.baseline) || entry.name !== entry.baselineName;
  const missingParentsFor = entry => [...new Set([...(remote(entry)?.missingParents || []),
    ...entry.baseHeads.filter(id=>!records.has(id)),...(entry.pending?.payload.parents || []).filter(id=>!records.has(id))])].sort(compareText);
  const conflict = entry => !!remote(entry)?.conflict || !!missingParentsFor(entry).length ||
    ((dirty(entry) || !!entry.pending) && !same(remote(entry)?.heads || [], entry.baseHeads));
  function statusFor(entry) {
    return {key: entry.key, scope: copyJson(entry.scope), name: entry.name, loaded: entry.loaded, loading: entry.loading,
      autoSave: entry.scope.viewId === PERSONAL_VIEW_ID, dirty: dirty(entry), saving: !!entry.inFlight,
      pending: !!entry.pending, conflict: conflict(entry), conflictRevisions: remote(entry)?.heads.slice() || [],
      missingParents: missingParentsFor(entry), error: entry.error || journalError,
      journalEnabled: !!journal, journalError,
      canUndo: entry.undo.length > 0, canRedo: entry.redo.length > 0,
      revisionId: remote(entry)?.revisionId || null, updatedAt: remote(entry)?.updatedAt || null};
  }
  /** Export only the per-user draft recovery journal, not the remote catalog.
   * Main callers: injected journal.write and reload recovery tests. Baseline
   * IDs describe what an edit was based on; loading this envelope does not
   * acknowledge them or make a restored view loaded.
   */
  function exportJournal() {
    return copyJson({version:1,userId:actorId,views:[...sessions.values()].map(entry=>({
      scope:entry.scope,name:entry.name,draft:entry.draft,baseline:entry.baseline,
      baselineName:entry.baselineName,baseHeads:entry.baseHeads,pending:entry.pending,
      undo:entry.undo,redo:entry.redo,error:entry.error,
    }))});
  }
  /** Validate a whole device journal before replacing any existing draft.
   * Main callers: store creation and explicit recovery import. A different
   * signed-in user cannot replay a pending save from this device. Pending
   * envelopes retain every byte of their canonical transport representation.
   */
  function restoreJournal(envelope) {
    if([...sessions.values()].some(entry=>entry.inFlight || entry.loading))throw new Error('Wait for the workspace request before restoring its recovery journal.');
    const saved=copyJson(envelope), candidate=new Map(), pendingIds=new Set();
    if(saved?.version!==1 || saved.userId!==actorId || !Array.isArray(saved.views))throw new Error('The workspace recovery journal belongs to another user or has an unsupported format.');
    for(const view of saved.views) {
      const scope=checkedScope(view.scope),key=workspaceKey(scope);
      if(scope.viewId===PERSONAL_VIEW_ID && scope.userId!==actorId)throw new Error('The recovery journal contains another user\'s personal workspace.');
      if(candidate.has(key))throw new Error('The recovery journal contains duplicate workspace identities.');
      if(!Array.isArray(view.baseHeads) || view.baseHeads.length>64 || new Set(view.baseHeads).size!==view.baseHeads.length)throw new Error('The recovery journal has invalid baseline revisions.');
      view.baseHeads.forEach(id=>requiredText(id,'baseline revision'));
      if(!Array.isArray(view.undo) || !Array.isArray(view.redo) || view.undo.length>historyLimit || view.redo.length>historyLimit || typeof view.error!=='string')throw new Error('The recovery journal has invalid edit history.');
      let pending=null;
      if(view.pending!==null) {
        const pendingCatalog=replayWorkspaces([view.pending]);
        if(pendingCatalog.size!==1 || !pendingCatalog.has(key) || view.pending.payload.actorId!==actorId || !same(view.pending.payload.scope,scope))throw new Error('The recovery journal has a pending save for another identity.');
        const canonicalPending=makeWorkspaceEvent({...view.pending.payload,eventId:view.pending.event_id,timestamp:view.pending.timestamp});
        if(!same(canonicalPending,view.pending) || pendingIds.has(view.pending.event_id))throw new Error('The recovery journal has an invalid or duplicate pending revision.');
        pendingIds.add(view.pending.event_id);
        pending=copyJson(view.pending);
      }
      candidate.set(key,{key,scope,name:checkedName(view.name),baselineName:checkedName(view.baselineName),
        draft:snapshotJson(view.draft),baseline:snapshotJson(view.baseline),baseHeads:view.baseHeads.slice(),
        pending,undo:view.undo.map(snapshotJson),redo:view.redo.map(snapshotJson),error:view.error,
        timer:null,loaded:false,loading:false,inFlight:null,saveRequested:false,resolveRequested:false});
    }
    for(const entry of sessions.values())stopTimer(entry);
    sessions.clear();for(const [key,entry] of candidate)sessions.set(key,entry);
    if(!sessions.has(activeKey))activeKey=null;
    if(activeKey)notify(active());
    return exportJournal();
  }
  function persistJournal() {
    if(!journal)return true;
    try {
      const result=journal.write(exportJournal());
      if(result && typeof result.then==='function')throw new Error('Journal writes must be synchronous.');
      journalError='';return true;
    } catch(error) {journalError=`Workspace reload recovery could not be written: ${error.message || 'Device storage is unavailable.'}`;return false;}
  }
  function notify(entry) {
    persistJournal();
    if (activeKey === entry.key) onChange({snapshot: copyJson(entry.draft), status: statusFor(entry)});
  }
  function stopTimer(entry) { if (entry.timer !== null) cancel(entry.timer); entry.timer = null; }
  function queueAutoSave(entry) {
    stopTimer(entry);
    if (entry.scope.viewId !== PERSONAL_VIEW_ID || (!entry.pending && !dirty(entry)) || conflict(entry) || !entry.loaded || entry.error || journalError) return;
    entry.timer = schedule(() => { entry.timer = null; write(entry).catch(() => {}); }, debounceMs);
  }
  function ingest(events) {
    if (!Array.isArray(events)) throw new Error('Workspace load must return an event array.');
    // Validate the entire candidate before replacing any acknowledged state.
    const candidate = replayWorkspaces([...records.values(), ...events]);
    for(const entry of sessions.values())if(entry.pending) {
      for(const raw of events)if(raw?.event_id===entry.pending.event_id && isWorkspaceEvent(raw)) {
        const payload=typeof raw.payload==='string' ? JSON.parse(raw.payload) : raw.payload;
        const acknowledged=makeWorkspaceEvent({...payload,eventId:raw.event_id,timestamp:raw.timestamp});
        if(!same(acknowledged,entry.pending))throw new Error('The pending workspace save conflicts with its remote revision.');
      }
    }
    for (const event of events) if ((event?.type || event?.event_type) === WORKSPACE_EVENT_TYPE) {
      const payload = typeof event.payload === 'string' ? JSON.parse(event.payload) : event.payload;
      records.set(event.event_id, makeWorkspaceEvent({...payload, eventId: event.event_id, timestamp: event.timestamp}));
    }
    catalog = candidate;
    for (const entry of sessions.values()) {
      // The first request may have succeeded despite a lost HTTP response.
      // A real read of that exact event acknowledges it without republishing.
      const acknowledged=entry.pending && records.get(entry.pending.event_id);
      if(acknowledged) {
        if(!same(acknowledged,entry.pending))throw new Error('The pending workspace save conflicts with its remote revision.');
        entry.baseline=copyJson(acknowledged.payload.snapshot);entry.baselineName=acknowledged.payload.name;
        entry.baseHeads=[acknowledged.event_id];entry.pending=null;entry.error='';
      }
      const saved = remote(entry);
      const canAdopt = saved && (!saved.conflict && !saved.missingParents.length || !entry.loaded && !entry.baseHeads.length);
      if (canAdopt && !dirty(entry) && !entry.pending && !same(saved.heads, entry.baseHeads)) {
        entry.scope = copyJson(saved.scope); entry.name = saved.name; entry.baselineName = saved.name; entry.draft = copyJson(saved.snapshot);
        entry.baseline = copyJson(saved.snapshot); entry.baseHeads = saved.heads.slice();
        entry.undo = []; entry.redo = [];
      }
      if (conflict(entry)) stopTimer(entry);
      notify(entry);
    }
    return new Map([...catalog].map(([key, value]) => [key, copyJson(value)]));
  }
  async function refreshEntry(entry) {
    entry.loading = true; notify(entry);
    try { ingest(await load(copyJson(entry.scope))); entry.loaded = true; }
    finally { entry.loading = false; notify(entry); }
  }
  async function open(scope, {snapshot = {}, name = scope?.viewId === PERSONAL_VIEW_ID ? 'Personal workspace' : 'Workspace'} = {}) {
    const value = checkedScope(scope), key = workspaceKey(value);
    if (value.viewId === PERSONAL_VIEW_ID && value.userId !== actorId) throw new Error('A personal workspace belongs to another user.');
    let entry = sessions.get(key);
    if (!entry) {
      const initial = snapshotJson(snapshot);
      entry = {key, scope: value, name: checkedName(name), baselineName: checkedName(name), draft: initial, baseline: copyJson(initial), baseHeads: [],
        undo: [], redo: [], timer: null, loaded: false, loading: false, pending: null, inFlight: null, error: '', saveRequested: false, resolveRequested: false};
      sessions.set(key, entry);
    }
    activeKey = key; notify(entry);
    try { await refreshEntry(entry); if(!entry.pending && !dirty(entry))entry.error = ''; }
    catch (error) { entry.error = error.message || 'Could not load saved workspace settings.'; notify(entry); throw error; }
    notify(entry);queueAutoSave(entry);
    return {snapshot: copyJson(entry.draft), status: statusFor(entry)};
  }
  function edit(snapshot) {
    const entry = active(), next = snapshotJson(snapshot);
    if (same(entry.draft, next)) return false;
    entry.undo.push(copyJson(entry.draft)); if (entry.undo.length > historyLimit) entry.undo.shift();
    entry.redo = []; entry.draft = next; notify(entry); queueAutoSave(entry); return true;
  }
  function historyMove(from, to) {
    const entry = active();
    if (!entry[from].length) return false;
    entry[to].push(copyJson(entry.draft)); entry.draft = entry[from].pop();
    notify(entry); queueAutoSave(entry); return true;
  }
  function write(entry, {name, resolveConflict = false, explicit = false} = {}) {
    if (name !== undefined) entry.name = checkedName(name);
    if (explicit) entry.saveRequested = true;
    if (resolveConflict) entry.resolveRequested = true;
    stopTimer(entry);
    if (entry.inFlight) return entry.inFlight;
    if (!entry.pending && !entry.saveRequested && !(entry.scope.viewId === PERSONAL_VIEW_ID && dirty(entry))) return Promise.resolve(statusFor(entry));
    entry.error = '';
    entry.inFlight = (async () => {
      try {
        if(entry.pending) {
          await refreshEntry(entry);
          if(!entry.pending && !dirty(entry) && !entry.resolveRequested)entry.saveRequested=false;
        } else if(!entry.loaded)await refreshEntry(entry);
        while (entry.pending || entry.saveRequested || entry.scope.viewId === PERSONAL_VIEW_ID && dirty(entry)) {
          const forced = entry.saveRequested; entry.saveRequested = false;
          if (entry.pending && forced && (!same(entry.draft, entry.pending.payload.snapshot) || entry.name !== entry.pending.payload.name)) entry.saveRequested = true;
          if (!entry.pending) {
            await refreshEntry(entry);
            if (missingParentsFor(entry).length) throw new Error('Saved workspace history is incomplete. Refresh before saving.');
            if (conflict(entry) && !entry.resolveRequested) throw new Error('This workspace changed elsewhere. Reload or explicitly resolve the conflict before saving.');
            if (!dirty(entry) && !forced) break;
            const saved = remote(entry);
            // Preserve the creator metadata even when a teammate saves a view.
            if (saved) entry.scope = copyJson(saved.scope);
            entry.pending = makeWorkspaceEvent({scope: entry.scope, snapshot: entry.draft, name: entry.name,
              parents: saved?.heads || [], actorId, eventId: newId(), timestamp: clock()});
            entry.resolveRequested = false;
          }
          notify(entry);
          if(missingParentsFor(entry).length)throw new Error('Saved workspace history is incomplete. Refresh before retrying this save.');
          if(!persistJournal())throw new Error(journalError);
          const pending = entry.pending;
          await publish(copyJson(pending));
          // Advance the base only for the exact acknowledged snapshot. Edits
          // made while publishing remain dirty and keep their undo history.
          entry.baseline = copyJson(pending.payload.snapshot); entry.baselineName = pending.payload.name; entry.baseHeads = [pending.event_id];
          entry.pending = null; ingest([pending]); entry.error = ''; notify(entry);
          if(!persistJournal())throw new Error(journalError);
          if (conflict(entry)) throw new Error('Another save conflicts with this version. Both revisions were retained.');
          if (entry.scope.viewId !== PERSONAL_VIEW_ID && !entry.saveRequested) break;
        }
        return {...statusFor(entry), saving: false};
      } catch (error) {
        entry.error = error.message || 'Could not save workspace settings to Foundry.'; notify(entry); throw error;
      } finally { entry.inFlight = null; if (!dirty(entry)) stopTimer(entry); notify(entry); }
    })();
    notify(entry);
    return entry.inFlight;
  }
  /** Abandon a local draft and adopt an explicitly chosen remote display.
   * Main callers: Clear changes and conflict recovery. Unlike reverting to the
   * old local baseline, this follows the latest known remote revision. When
   * remote history itself has branches, the caller must choose a revision;
   * choosing its display does not resolve those cloud branches by itself.
   */
  function adoptRemote({revisionId}={}) {
    const entry=active(),saved=remote(entry);
    if(entry.inFlight || entry.loading)throw new Error('Wait for the workspace request before discarding its draft.');
    if(missingParentsFor(entry).length)throw new Error('Saved workspace history is incomplete. Reload before discarding changes.');
    if(saved?.conflict && !revisionId)throw new Error('Choose a remote revision before discarding conflicting changes.');
    if(revisionId && !saved?.heads.includes(revisionId))throw new Error('That workspace revision is not a current remote version.');
    const chosen=revisionId ? records.get(revisionId) : saved && records.get(saved.revisionId);
    const next=chosen ? chosen.payload.snapshot : entry.baseline, name=chosen ? chosen.payload.name : entry.baselineName;
    const changed=!same(entry.draft,next) || entry.name!==name || !!entry.pending;
    stopTimer(entry);entry.draft=copyJson(next);entry.baseline=copyJson(next);entry.name=name;entry.baselineName=name;
    entry.baseHeads=saved?.heads.slice() || [];if(chosen)entry.scope=copyJson(chosen.payload.scope);
    entry.pending=null;entry.error='';entry.undo=[];entry.redo=[];entry.saveRequested=false;entry.resolveRequested=false;
    notify(entry);return changed;
  }
  /** Refresh authenticated archive/live records, preserving a draft by default.
   * Main callers: Reload and conflict resolution. discardDraft is an explicit
   * request to adopt the freshly read remote display, never an autosave policy.
   */
  async function reload({discardDraft=false,revisionId}={}) {
    const entry=active();
    if(entry.inFlight || entry.loading)throw new Error('Wait for the current workspace request before reloading.');
    try {await refreshEntry(entry);if(discardDraft)adoptRemote({revisionId});else if(!entry.pending)entry.error='';}
    catch(error){entry.error=error.message || 'Could not reload saved workspace settings.';notify(entry);throw error;}
    notify(entry);queueAutoSave(entry);return {snapshot:copyJson(entry.draft),status:statusFor(entry)};
  }
  if(journal) {
    const initial=journal.read();
    if(initial && typeof initial.then==='function')throw new Error('Workspace recovery journal reads must be synchronous.');
    if(initial!==null && initial!==undefined)restoreJournal(initial);
  }
  /** Enumerate recoverable device drafts that have no observed cloud revision.
   * Main callers: workspace-session's view selector and failed Save-as recovery.
   * localDraft is explicit: these entries cannot be presented as saved views.
   * Their creator scope is retained so opening one cannot change its identity.
   */
  function recoverableViews(entity,project) {
    return [...sessions.values()].filter(entry=>!catalog.has(entry.key) && entry.scope.entity===entity && entry.scope.project===project &&
      (dirty(entry) || entry.pending || entry.error)).map(entry=>({key:entry.key,scope:copyJson(entry.scope),name:entry.name,
      snapshot:copyJson(entry.draft),revisionId:null,updatedAt:null,heads:[],conflict:conflict(entry),missingParents:missingParentsFor(entry),
      localDraft:true,pending:!!entry.pending,error:entry.error || journalError}));
  }
  return {open, edit, ingest,
    save: options => write(active(), {...options, explicit: true}),
    flush: () => write(active()),
    undo: () => historyMove('undo', 'redo'), redo: () => historyMove('redo', 'undo'), reset: edit,
    discard: () => adoptRemote(), adoptRemote, reload, exportJournal, restoreJournal, recoverableViews,
    current: () => copyJson(active().draft), status: () => statusFor(active()),
    views: (entity, project) => [...catalog.values()].filter(view => view.scope.entity === entity && view.scope.project === project &&
      (view.scope.viewId !== PERSONAL_VIEW_ID || view.scope.userId === actorId)).map(copyJson),
    dispose: () => { for (const entry of sessions.values()) stopTimer(entry); },
  };
}
