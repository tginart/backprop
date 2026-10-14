/**
 * Architecture: project display choices -> complete JSON snapshot -> revision
 * journal -> restore display choices for that same project.
 * Main callers: workspace-session and the app's project/view selector.
 * Invariants: checkboxes, run data, credentials and navigation never enter a
 * snapshot. Visibility uses hiddenRuns, independently of checkbox selection.
 * Pagination, chart zoom and temporary legend hiding are transient. Save
 * acknowledgments can preserve those through restoreView's preserveTransient.
 * Why this exists: one display boundary prevents settings from another project
 * leaking into a fresh personal workspace or an explicitly saved shared view.
 */
import {newLayout} from './workspace-layout.mjs';

const scalarDefaults = {
  xAxis:'defined', smoothing:0, smoothingMethod:'twema', logScale:false,
  columns:3, runColumns:null, search:'', status:'', group:'', metricSearch:'',
  metricRegex:false, richRun:'', richType:'', richSearch:'', compareSearch:'',
  differences:false,
};
const projectMaps = ['panelSettings','hiddenPanels','pinnedPanels','collapsedSections'];
const runMaps = ['richSteps','tableOptions','tableFilters'];
const analysisDefaults = () => ({filters:[],sort:'run:created_at',direction:-1,groupBy:'',aggregation:'mean',range:'stddev',searchRegex:false});
const fail = message => {throw new Error(message);};
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
const strings = values => Array.isArray(values) && values.every(value => typeof value === 'string');

/** Copy inert JSON, rejecting values JSON.stringify would silently change.
 * Main callers: captureView and validateView. Runtime optional object settings
 * may be undefined, meaning "inherit"; capture omits those keys deliberately.
 * Stored snapshots must already be JSON and cannot contain undefined or NaN.
 */
function jsonCopy(value, omitOptional = false, path = 'view', ancestors = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'object' || ancestors.has(value) || (!Array.isArray(value) && !object(value))) fail(`The saved view contains a non-JSON value at ${path}.`);
  ancestors.add(value);
  let result;
  if (Array.isArray(value)) result = Array.from(value, (item,index) => jsonCopy(item,omitOptional,`${path}[${index}]`,ancestors));
  else result = Object.fromEntries(Object.entries(value).filter(([,item]) => !omitOptional || item !== undefined).map(([key,item]) => [key,jsonCopy(item,omitOptional,`${path}.${key}`,ancestors)]));
  ancestors.delete(value);
  return result;
}
function owningProject(id, project) {
  if (typeof id !== 'string') return false;
  try {const key=JSON.parse(id);return Array.isArray(key) && key.length === 2 && key[0] === project && typeof key[1] === 'string';}
  catch {return false;}
}
const owningRun = (key,project) => typeof key === 'string' && key.startsWith(`${project}/`) && key.length > project.length+1;

/** Identify only stable logged-output keys, including nested table suffixes.
 * Main callers: capture/restore. Data-page keys start with run.key; panel-bank
 * keys encode [panelId, run.key, writer]. Explorer counter IDs are temporary.
 */
function outputRun(id, project, runKeys = []) {
  if (typeof id !== 'string') return null;
  if (id.startsWith('[')) {
    // A nested table appends :row:column after the encoded panel output key.
    const end=id.lastIndexOf(']');
    if (end >= 0) {
      try {const key=JSON.parse(id.slice(0,end+1));if(Array.isArray(key) && key.length===3 && typeof key[0]==='string' && owningRun(key[1],project) && (key[2]===null || typeof key[2]==='string') && (!id.slice(end+1) || id.slice(end+1).startsWith(':')))return key[1];}
      catch {/* Not a stable encoded output identity. */}
    }
  }
  const raw=id.startsWith('summary:') ? id.slice(8) : id;
  const known=runKeys.find(key => raw.startsWith(`${key}:`));
  if(known)return known;
  const end=raw.indexOf(':');
  const run=end < 0 ? '' : raw.slice(0,end);
  return owningRun(run,project) ? run : null;
}
function pairs(values,label) {
  if(!Array.isArray(values) || values.some(item => !Array.isArray(item) || item.length!==2 || typeof item[0]!=='string') || new Set(values.map(item=>item[0])).size!==values.length)fail(`The saved view has invalid ${label}.`);
  return values;
}

/** Return a new, complete project display with no inherited global choices.
 * Main callers: workspace-session.defaults and captureView for absent fields.
 * Each call owns its arrays and objects, so edits cannot change other defaults.
 */
export function defaultView(project) {
  if(typeof project !== 'string' || !project)fail('Choose a project before opening a workspace view.');
  return {...scalarDefaults,version:1,project,hiddenRuns:[],panelSettings:[],hiddenPanels:[],pinnedPanels:[],collapsedSections:[],analysis:analysisDefaults(),layout:newLayout(),colors:[],richSteps:[],tableOptions:[],tableFilters:[]};
}

/** Capture current display settings, keeping only this project's identities.
 * Main callers: workspace-session.edit/saveAs. Missing newly introduced state
 * uses explicit defaults; optional panel settings retain "inherit" semantics.
 * Exposing state.tableFilters lets the app include logged-table row searches.
 */
export function captureView(state, runKeys) {
  const snapshot=defaultView(state.project);
  for(const key of Object.keys(scalarDefaults))if(state[key]!==undefined)snapshot[key]=jsonCopy(state[key],true,key);
  if(snapshot.richRun && !owningRun(snapshot.richRun,state.project))snapshot.richRun='';
  snapshot.hiddenRuns=[...(state.hiddenRuns || [])].filter(key => owningRun(key,state.project));
  for(const key of projectMaps) {
    const collection=state[key] || (key==='panelSettings' ? new Map() : new Set());
    snapshot[key]=[...collection].filter(item => owningProject(collection instanceof Map ? item[0] : item,state.project));
  }
  snapshot.analysis={...analysisDefaults(),...state.analysisPreferences?.get(state.project)};
  snapshot.layout={...newLayout(),...state.workspaceLayouts?.get(state.project)};
  snapshot.colors=[...(state.runColors || [])].filter(([key]) => owningRun(key,state.project));
  for(const key of runMaps)snapshot[key]=[...(state[key] || [])].filter(([id]) => outputRun(id,state.project,runKeys));
  return validateView(jsonCopy(snapshot,true),state.project);
}

/** Normalize and validate before changing any active display state.
 * Main callers: restoreView and local import. Existing version-1 visibility
 * snapshots receive defaults for new display fields. Old selected-only device
 * views require explicit migrateLocalView; cloud data is never silently migrated.
 */
export function validateView(snapshot, project) {
  if(snapshot?.version!==1 || !Number.isInteger(snapshot.columns) || snapshot.columns<1 || snapshot.columns>3)fail('This saved workspace view has an unsupported format.');
  if(!Array.isArray(snapshot.hiddenRuns) && Array.isArray(snapshot.selected))fail('This legacy device view requires explicit visibility migration before it can be used.');
  const saved=jsonCopy(snapshot);
  if(saved.project!==undefined && saved.project!==project)fail('The saved view contains settings for a different project.');
  for(const key of ['selected','selectedRows','rowSelection','checkedRuns','runs','events','model','auth','accessToken','refreshToken'])if(Object.hasOwn(saved,key))fail(`The saved view cannot contain ${key}.`);
  const allowed=new Set(Object.keys(defaultView(project)));
  for(const key of Object.keys(saved))if(!allowed.has(key))fail(`The saved view has an unsupported field: ${key}.`);
  for(const key of ['hiddenRuns',...projectMaps,'colors'])if(!Array.isArray(saved[key]))fail('The saved view is incomplete.');
  if(!object(saved.analysis) || !object(saved.layout))fail('The saved view has no analysis or layout settings.');
  const normalized={...defaultView(project),...saved,project,analysis:{...analysisDefaults(),...saved.analysis},layout:{...newLayout(),...saved.layout}};
  for(const [key,value] of Object.entries(scalarDefaults)) {
    const supplied=normalized[key];
    if(key==='runColumns') {if(supplied!==null && !strings(supplied))fail('The saved view has invalid run columns.');}
    else if(typeof supplied!==typeof value)fail(`The saved view has invalid ${key}.`);
  }
  if(!normalized.xAxis || !['ema','twema'].includes(normalized.smoothingMethod) || normalized.smoothing<0 || normalized.smoothing>1)fail('The saved view has invalid chart settings.');
  if(!strings(normalized.hiddenRuns) || normalized.hiddenRuns.some(key=>!owningRun(key,project)) || (normalized.richRun && !owningRun(normalized.richRun,project)))fail('The saved view contains runs for a different project.');
  for(const key of projectMaps) {
    const entries=key==='panelSettings' ? pairs(normalized[key],'panel settings') : normalized[key];
    if(key!=='panelSettings' && !strings(entries))fail(`The saved view has invalid ${key}.`);
    for(const item of entries)if(!owningProject(key==='panelSettings' ? item[0] : item,project))fail('The saved view contains settings for a different project.');
    if(key==='panelSettings' && entries.some(([,settings])=>!object(settings)))fail('The saved view has invalid panel settings.');
  }
  for(const [key,color] of pairs(normalized.colors,'run colors')) {
    if(!owningRun(key,project))fail('The saved view contains runs for a different project.');
    if(typeof color!=='string' || !/^#[0-9a-f]{6}$/i.test(color))fail(`The saved view has an invalid run color for ${key}.`);
  }
  for(const key of runMaps)for(const [id,value] of pairs(normalized[key],key)) {
    if(!outputRun(id,project))fail('The saved view contains logged-output settings for a different project or a temporary dialog.');
    if(key==='richSteps' && typeof value!=='string' && typeof value!=='number')fail('The saved view has an invalid logged-data step.');
    if(key==='tableOptions' && !object(value))fail('The saved view has invalid table options.');
    if(key==='tableOptions') {
      if(value.sort!==undefined && (!Number.isInteger(value.sort) || value.sort<0))fail('The saved view has an invalid table sort column.');
      if(value.direction!==undefined && ![1,-1].includes(value.direction))fail('The saved view has an invalid table sort direction.');
      if(value.hiddenColumns!==undefined && (!Array.isArray(value.hiddenColumns) || value.hiddenColumns.some(index=>!Number.isInteger(index) || index<0)))fail('The saved view has invalid hidden table columns.');
      if(value.limit!==undefined && ![25,50,100].includes(value.limit))fail('The saved view has an invalid table row limit.');
      if(value.column!==undefined && !(value.column==='' || /^\d+$/.test(String(value.column))))fail('The saved view has an invalid table search column.');
    }
    if(key==='tableFilters' && typeof value!=='string')fail('The saved view has an invalid table search.');
  }
  if(!Array.isArray(normalized.analysis.filters) || ![1,-1].includes(normalized.analysis.direction))fail('The saved view has invalid analysis settings.');
  for(const key of ['pinnedColumns','pinnedRuns','panelOrder','panelOptions','sectionOrder','sections','copies'])if(!Array.isArray(normalized.layout[key]))fail('The saved view has invalid layout settings.');
  for(const key of ['pinnedColumns','pinnedRuns','panelOrder','sectionOrder'])if(!strings(normalized.layout[key]))fail('The saved view has invalid layout identities.');
  if(normalized.layout.pinnedRuns.some(key=>!owningRun(key,project)))fail('The saved view contains pinned runs for a different project.');
  if(pairs(normalized.layout.panelOptions,'panel layout settings').some(([,options])=>!object(options)))fail('The saved view has invalid panel layout settings.');
  if(normalized.layout.sections.some(section=>!object(section) || typeof section.id!=='string' || (section.settings!==undefined && !object(section.settings))))fail('The saved view has invalid sections.');
  if(normalized.layout.copies.some(copy=>!object(copy) || typeof copy.id!=='string' || typeof copy.metric!=='string'))fail('The saved view has invalid panel copies.');
  return normalized;
}

/** Restore display choices while preserving all checkbox and navigation state.
 * Main callers: workspace-session.apply. Switching view resets transient chart
 * zoom, legend hiding and pagination; save acknowledgments pass
 * {preserveTransient:true} to avoid moving the user back to the first page.
 */
export function restoreView(state, snapshot, runKeys, {preserveTransient=false}={}) {
  const saved=validateView(snapshot,state.project);
  for(const key of Object.keys(scalarDefaults))state[key]=saved[key];
  state.hiddenRuns ||= new Set();
  for(const key of state.hiddenRuns)if(owningRun(key,state.project))state.hiddenRuns.delete(key);
  for(const key of saved.hiddenRuns)state.hiddenRuns.add(key);
  for(const key of projectMaps) {
    const collection=state[key] ||= (key==='panelSettings' ? new Map() : new Set());
    for(const id of collection.keys())if(owningProject(id,state.project))collection.delete(id);
    for(const item of saved[key])collection instanceof Map ? collection.set(...item) : collection.add(item);
  }
  (state.analysisPreferences ||= new Map()).set(state.project,saved.analysis);
  (state.workspaceLayouts ||= new Map()).set(state.project,saved.layout);
  state.runColors ||= new Map();
  for(const key of state.runColors.keys())if(owningRun(key,state.project))state.runColors.delete(key);
  for(const [key,color] of saved.colors)state.runColors.set(key,color);
  for(const key of runMaps) {
    const collection=state[key] ||= new Map();
    for(const id of collection.keys())if(outputRun(id,state.project,runKeys))collection.delete(id);
    for(const item of saved[key])collection.set(...item);
  }
  if(!preserveTransient) {state.zoom?.clear();state.hiddenSeries?.clear();state.tablePages?.clear();state.runPage=0;}
}

/** Convert the known, old device-only visibility format as an explicit import.
 * Main callers: the app's local preference migration. Its selected array meant
 * visible chart runs before checkboxes and visibility were separated. Only runs
 * known during import can be inverted; newly arriving runs remain visible.
 * This does not publish or mark anything saved, and never restores checkboxes.
 */
export function migrateLocalView(snapshot, project, runKeys) {
  if(snapshot?.version!==1 || !Array.isArray(snapshot.selected) || snapshot.hiddenRuns!==undefined)fail('This is not a selected-only legacy device view.');
  const selected=new Set(snapshot.selected), {selected:ignored,...display}=jsonCopy(snapshot);
  const migrated={...display,project,hiddenRuns:runKeys.filter(key=>owningRun(key,project) && !selected.has(key))};
  return {snapshot:validateView(migrated,project),warning:'Imported visibility from a legacy device view. Runs absent during import remain visible. These changes still require a Foundry save.'};
}
