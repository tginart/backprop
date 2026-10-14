/**
 * Architecture: authenticated identity -> project/view selection -> snapshot
 * journal -> Foundry acknowledgments -> the common workspace header.
 * Main callers: app boot, project/view navigation, display edits and Save.
 * Why this exists: the page renderer must not confuse a browser draft with a
 * saved view. Run selection and experiment records never enter this boundary.
 * Personal workspaces autosave; named views publish only after explicit Save.
 */
import {createWorkspaceStore, PERSONAL_VIEW_ID} from './workspace-store.mjs';

export function createWorkspaceSession({identity, read, publish, capture, apply, defaults, onChange, journal}) {
  let project = null, status = null, opening = false, applying = false, initialEvents = null;
  const store = createWorkspaceStore({userId:identity.userId,
    load:async () => {
      if (initialEvents) {const events=initialEvents;initialEvents=null;return events;}
      return read();
    }, publish, ...(journal ? {journal} : {}),
    onChange:change => {
      if (!project || change.status.scope.project !== project.name || change.status.scope.entity !== project.entity) return;
      status=change.status;
      let changed=false;
      if (JSON.stringify(capture()) !== JSON.stringify(change.snapshot)) {
        applying=true;
        try {apply(change.snapshot);changed=true;} finally {applying=false;}
      }
      onChange({status,changed,opening});
    }});
  const scope = viewId => ({entity:project.entity,project:project.name,userId:identity.userId,viewId});
  const views = () => project ? [...store.views(project.entity,project.name),...store.recoverableViews(project.entity,project.name)] : [];
  async function open(nextProject,viewId=PERSONAL_VIEW_ID,{events,seed}={}) {
    project=nextProject;opening=true;
    try {
      if(events){initialEvents=events;store.ingest(events);}
      const existing=views().find(view=>view.scope.viewId===viewId);
      if(viewId!==PERSONAL_VIEW_ID && !existing)throw new Error('This saved workspace view is unavailable. Choose your personal workspace to continue.');
      await store.open(existing?.scope || scope(viewId),{snapshot:seed || defaults(),name:existing?.name || 'Personal workspace'});
    } finally {opening=false;onChange({status,changed:true,opening});}
  }
  return {
    open, views, status:()=>status, identity,
    edit:()=>{if(status?.loaded && !opening && !applying)store.edit(capture());},
    ingest:events=>store.ingest(events),
    save:options=>store.save(options),
    undo:()=>store.undo(),redo:()=>store.redo(),discard:()=>store.discard(),reload:options=>store.reload(options),
    async adoptLatest(){await store.reload();const saved=views().find(view=>view.key===status.key);await store.reload({discardDraft:true,revisionId:saved?.revisionId});if(status.conflict)await store.save({resolveConflict:true});},
    async saveAs(name){
      const snapshot=capture();opening=true;
      try {await store.open(scope(crypto.randomUUID()),{snapshot,name});await store.save({name});}
      finally {opening=false;onChange({status,changed:true,opening});}
    },
    dispose:()=>store.dispose(),
  };
}

/** The same status is used in table, panel-bank and single-run headers.
 * Main callers: app savedViewControls. A successful write means acknowledged;
 * other clients will see it when archive/live reads catch up.
 */
export function workspaceSaveLabel(status, canSave) {
  if(!status)return 'Loading workspace…';
  if(status.conflict)return 'Conflicting changes';
  if(status.error)return 'Not saved';
  if(status.saving)return 'Saving…';
  if(status.loading)return 'Checking saved view…';
  if(status.dirty)return status.autoSave && canSave?'Waiting to save…':'Unsaved changes';
  if(status.revisionId)return 'Saved';
  return canSave?'No saved changes yet':'Saving not enabled';
}
