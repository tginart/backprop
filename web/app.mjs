import {withDeadline} from './network.mjs';
import {CUSTOM_WORKSPACES, EXPLORATION_STORAGE_KEY} from './experience.mjs';
import {renderLoggedPanel as renderTypedPanel,openPanelLibrary as showPanelLibrary,openLoggedPanelEditor as showLoggedPanelEditor} from './panel-bank.mjs';
import {discoverPanelSources,materializePanels,createPanel,panelVersions,resolvePanelData} from './panel-catalog.mjs';
import { installTooltips, openActionMenu } from './interaction-overlays.mjs';
import { completionText } from './history-completeness.mjs';
import { hydrateVega } from './vega-view.mjs';
const vegaRegistry = new Map();
/**
 * Architecture: API/auth boundary -> canonical run model -> view state -> DOM.
 * The API layer owns credentials, retries, archival reads and stream offsets.
 * This module owns workspace choices and renders only data returned by Foundry.
 * Default browsing choices stay in this browser. Custom workspace code is gated.
 * Run data is never synthesized in the production UI.
 * Main entry points: boot(), refresh(), delegated click/input/change handlers.
 */
import { initialize, loadWorkspace, getMediaUrl, loadRunControl, publishRunStop, getWorkspaceIdentity, publishWorkspaceEvent, isWorkspaceEvent } from './api.js';
import { getAuthState, login, logout } from './auth.js';
import {createWorkspaceSession,workspaceSaveLabel} from './workspace-session.mjs';
import {captureViewState,restoreViewState,createRefreshRenderer} from './view-state.mjs';
import {renderProjectOverview,renderProjectArtifacts} from './project-pages.mjs';
import {renderRunComparer} from './run-comparer.mjs';
import {chartDefaultsFields,readChartDefaults} from './chart-defaults.mjs';
import { COLORS, normalizeEvents, metricKeys, seriesFor, tableAtStep, formatNumber as f, escapeHtml as e, finite, flatten, csv, projectKey, isRich, loggedValues, valueAtKey, definitionFor } from './model.mjs';
import { smoothSeries } from './smoothing.mjs';
import {splitRunWriters,effectiveAxis,groupedRunSeries} from './chart-series.mjs';
import { chartSpec, renderChart, bindChart } from './charts.mjs';
import { analysisFields, runField, selectAnalysisRuns, groupRuns, aggregateSeries } from './run-analysis.mjs';
import { openRunFilters } from './run-filter-editor.mjs';
import { newLayout, arrangePanels, panelOptions, setPanelOptions, sectionOptions, setSection, movePanel, bindWorkspaceLayout, defaultSection, ordered } from './workspace-layout.mjs';
import { captureView, restoreView, defaultView } from './saved-views.mjs';
import { panelKey, resolvePanel, tableRows, columnType, prioritizeConfigKeys, isPinnedConfigKey, comparisonRows } from './workspace.mjs';
import { hydratePlotly } from './plotly-view.mjs';
import { graphViewer } from './graph-view.mjs';
import { parseGraphJSON } from './graph-json.mjs';
import { openPanelEditor } from './panel-editor.mjs';
import { imageViewer, replaceView, watchMediaErrors } from './media-view.mjs';
import { renderConsole } from './console-view.mjs';
import { createRunControl } from './run-control.mjs';
import { createTableExplorer, isArtifactTable, snapshotTables, tableRelation, nestedTableRelations } from './table-explorer.mjs';

const icons = {
  settings:'<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3" fill="var(--surface)"/><circle cx="15" cy="17" r="3" fill="var(--surface)"/>',
  pin:'<path d="m9 3 6 0-1 7 4 4H6l4-4-1-7m3 11v7"/>',
  eye:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12"/><circle cx="12" cy="12" r="3"/>',
  left:'<path d="m15 5-7 7 7 7"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  runs:'<path d="M9 5h12M9 12h12M9 19h12"/><rect x="3" y="4" width="2" height="2"/><rect x="3" y="11" width="2" height="2"/><rect x="3" y="18" width="2" height="2"/>',
  compare:'<path d="M4 4v16M12 4v16M20 4v16M4 8l8 7 8-10M4 17l8-9 8 9"/>',
  data:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
  code:'<path d="m8 5-6 7 6 7m8-14 6 7-6 7m-3-17-2 20"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  refresh:'<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',
  expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
  reset:'<path d="M4 10a8 8 0 1 1 1 9M4 4v6h6"/>',
  copy:'<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M15 8V4H4v11h4"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4m-4 5v2"/>',
  pulse:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  check:'<path d="m5 12 4 4L20 5"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.grid}</svg>`;
const app = document.querySelector('#app');
const hideControlTooltip = installTooltips();
const detailDialog = document.querySelector('#detail-dialog');
const chartDialog = document.querySelector('#chart-dialog');
const expandedChartObserver = new ResizeObserver(entries => {
  for (const {target} of entries) {
    const svg = target.querySelector('.chart-svg');
    if (!chartDialog.open || !svg) continue;
    const viewport = svg.viewBox.baseVal;
    if (viewport.width !== svg.clientWidth || viewport.height !== svg.clientHeight) {
      bindCharts(chartDialog);
    }
  }
});
chartDialog.addEventListener('close', () => expandedChartObserver.disconnect());
const panelDialog = document.querySelector('#panel-dialog');
const inspectDialog = document.querySelector('#inspect-dialog');
const tableDialog = document.querySelector('#table-dialog');
const controlDialog = document.querySelector('#control-dialog');
let preferences = {};
try { preferences = JSON.parse(localStorage.getItem(EXPLORATION_STORAGE_KEY) || '{}'); } catch { /* Invalid local preferences never invalidate server data. */ }
let preferenceError = '';
let model = { runs: [], projects: [], issues: [], eventCount: 0 };
const state = { project: preferences.project || '', view: CUSTOM_WORKSPACES ? preferences.view || 'workspace' : 'workspace', selected: new Set(), hiddenRuns: new Set(preferences.hiddenRuns || []),
  search: preferences.search || '', status: preferences.status || '', group: preferences.group || '', metricSearch: preferences.metricSearch || '', metricRegex: preferences.metricRegex || false, xAxis: preferences.xAxis || 'defined', smoothing: preferences.smoothing || 0,
  smoothingMethod:preferences.smoothingMethod || 'twema', logScale: preferences.logScale || false, columns: preferences.columns || 3, live: true, sort: 'created_at', sortDir: -1,
  loading: true, refreshing: false, error: '', warning: '', fetchedAt: null, source: '', zoom: new Map(),
  richRun: '', richType: '', differences: false, compareSearch: '', richSteps: new Map(), initialized: false, dialogTab: 'overview', runKey: preferences.runKey || '', runTab: preferences.runTab || 'charts',
  auth: {}, selectedInitialized: new Set(preferences.selectedProjects || []), runPage: 0, tablePages: new Map(),
  panelSettings: new Map(preferences.panelSettings || []), hiddenPanels: new Set(preferences.hiddenPanels || []), pinnedPanels: new Set(preferences.pinnedPanels || []),
  collapsedSections: new Set(preferences.collapsedSections || []), hiddenSeries: new Map(), tableOptions: new Map(), richSearch: '',
  compactNavigation: preferences.presentationVersion === 2 ? preferences.compactNavigation ?? true : true, runColumns: preferences.runColumns || null };
state.analysisPreferences = new Map(preferences.analysisPreferences || []);
state.runColors = new Map(preferences.runColors || []);
state.workspaceLayouts = new Map(preferences.workspaceLayouts || []);
state.namedViews = new Map(preferences.namedViews || []);
state.activeViews = new Map(preferences.activeViews || []);
state.personalViews = new Map(preferences.personalViews || []);
state.artifactSearch='';state.artifactSelection={};
const chartRegistry = new Map();
const expandedChartEntries = new WeakMap();
const mediaRegistry = new Map();
const plotlyRegistry = new Map();
const richRegistry = new Map();
const tableRegistry = new Map();
const tableContexts = new Map();
const artifactTableRegistry = new Map();
const tableFilters = new Map();
state.tableFilters=tableFilters;
let mediaSequence = 0;
let appliedRunLink = false;
let refreshTimer, toastTimer, request = null, applicationConfig = null;
let renderedViewScope = '', renderRevision = 0;
const refreshRenderer = createRefreshRenderer(app, () => render());
let workspaceSession=null,workspaceSessionError='',workspaceRead=Promise.resolve();
/** Serialize archive/live reads so autosave and the experiment refresh loop
 * cannot race their shared stream offsets. Main callers: refresh and view Save.
 */
function readWorkspace(options={}) {
  const result=workspaceRead.then(()=>loadWorkspace(options));
  workspaceRead=result.catch(()=>{});return result;
}
function currentSnapshot(){analysis();layout();return captureView(state,projectRuns().map(run=>run.key));}
function workspaceChanged({status,changed,opening}){
  if(status)state.activeViews.set(state.project,status.scope.viewId);
  if(changed){renderedCatalog=null;if(!opening)render();}
  else {const header=document.querySelector('.workspace-chrome');if(header)header.outerHTML=renderWorkspaceHeading();}
}
async function openWorkspaceView(viewId='personal',{events,seed}={}){
  if(!chosenProject())return;
  if(!CUSTOM_WORKSPACES){updateLink();render();return;}
  workspaceSessionError='';
  try{
    if(!workspaceSession){
      const identity=await getWorkspaceIdentity();
      const journalKey=`backprop.workspace-journal.v1:${identity.userId}`;
      workspaceSession=createWorkspaceSession({identity,journal:{read:()=>{const value=localStorage.getItem(journalKey);return value?JSON.parse(value):null;},write:value=>localStorage.setItem(journalKey,JSON.stringify(value))},
        read:async()=>(await readWorkspace({refreshArchive:true})).events.filter(isWorkspaceEvent),publish:publishWorkspaceEvent,
        capture:currentSnapshot,apply:snapshot=>restoreView(state,snapshot,projectRuns().map(run=>run.key),{preserveTransient:true}),
        defaults:()=>defaultView(state.project),onChange:workspaceChanged});
    }
    await workspaceSession.open(chosenProject(),viewId,{events,seed});
    updateLink();render();
  }catch(error){workspaceSessionError=error.message;render();}
}

/** Read through the existing authenticated, verified artifact-file transport.
 * Main callers: the table explorer's JSON and NumPy decoders. A saved table
 * cannot provide its own network URL or bypass the Foundry manifest lookup.
 */
async function readArtifactResponse(file) {
  const url=await getMediaUrl(file);
  if(!url.startsWith('blob:'))throw new Error('The file API returned an unexpected URL.');
  const response=await fetch(url);
  if(!response.ok)throw new Error('Could not read the artifact Table file.');
  return response;
}
const tableExplorer = createTableExplorer({
  dialog: tableDialog, replace: replaceView, icon, renderTable, renderRow: renderRowFields, hydrate: hydrateMedia,
  download: (table, title) => download(JSON.stringify(table, null, 2), `${fileName(title)}.json`, 'application/json'),
  rowsFor: (table, id) => tableRows(table, { ...(state.tableOptions.get(id) || {}), search: tableFilters.get(id) || '' }),
  register: frame => { tableRegistry.set(frame.id, frame.table); tableContexts.set(frame.id, { ...frame.context, title: frame.title }); },
  readJson: async file => (await readArtifactResponse(file)).json(),
  readBytes: async file => (await readArtifactResponse(file)).arrayBuffer(),
});

const runControl = createRunControl({
  dialog: controlDialog, read: loadRunControl, publish: publishRunStop, auth: getAuthState, consent: login,
  onUpdate: status => {
    const run = model.runs.find(run => run.key === status.key);
    if (run) Object.assign(run, {control:status.control, stop_requests:status.stop_requests, state:status.state, finished_at:status.finished_at});
    if (detailDialog.open && detailDialog.dataset.run === status.key) openRun(status.key,state.dialogTab);
  },
});

function tableInExplorer(id) { return tableDialog.open && [...tableDialog.querySelectorAll('[data-table-search]')].some(node => node.dataset.tableSearch === id); }
function refreshTableView(id) {
  if (tableExplorer.has(id) || tableInExplorer(id)) tableExplorer.render();
  else if (detailDialog.open && [...detailDialog.querySelectorAll('[data-table-search]')].some(node => node.dataset.tableSearch === id)) openRun(detailDialog.dataset.run, state.dialogTab);
  else render();
}

function persist() {
  try{workspaceSession?.edit();}catch(error){workspaceSessionError=error.message;}
  const { project, view, xAxis, smoothing, smoothingMethod, logScale, columns, search, status, group } = state;
  try { localStorage.setItem(EXPLORATION_STORAGE_KEY, JSON.stringify({ presentationVersion: 2, project, view, xAxis, smoothing, smoothingMethod, logScale, columns, search, status, group, metricSearch:state.metricSearch, metricRegex:state.metricRegex, hiddenRuns: [...state.hiddenRuns], panelSettings: [...state.panelSettings], hiddenPanels: [...state.hiddenPanels], pinnedPanels: [...state.pinnedPanels], collapsedSections: [...state.collapsedSections], compactNavigation: state.compactNavigation, runColumns: state.runColumns, analysisPreferences: [...state.analysisPreferences], runColors: [...state.runColors], workspaceLayouts: [...state.workspaceLayouts], namedViews: [...state.namedViews], activeViews: [...state.activeViews], personalViews:[...state.personalViews], runKey:state.runKey, runTab:state.runTab })); preferenceError = ''; return true; } catch { preferenceError = 'Browser storage is unavailable. These workspace changes will not survive a reload.'; toast(preferenceError); return false; }
}
function updateLink(push = false) {
  const url = new URL(location.href), project = chosenProject();
  url.searchParams.delete('entity'); // Project IDs already include their entity.
  if (project) url.searchParams.set('project', project.id); else url.searchParams.delete('project');
  const run = state.view === 'run' ? projectRuns().find(item=>item.key===state.runKey) : null;
  if (run) url.searchParams.set('run',run.id); else url.searchParams.delete('run');
  if (state.view === 'run') url.searchParams.set('tab',state.runTab); else url.searchParams.delete('tab');
  for(const key of ['collection','version','artifactTab'])url.searchParams.delete(key);
  if(state.view==='artifacts')for(const [key,value] of Object.entries(state.artifactSelection))if(value)url.searchParams.set(key==='tab'?'artifactTab':key,value);
  url.searchParams.set('view', state.view);
  if(CUSTOM_WORKSPACES)url.searchParams.set('nw',state.activeViews.get(state.project) || 'personal');
  else url.searchParams.delete('nw');
  history[push ? 'pushState' : 'replaceState']({}, '', url);
  persist();
}
function toast(message) { const node = document.querySelector('#toast'); node.textContent = message; node.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => node.hidden = true, 3000); }
const chosenProject = () => model.projects.find(project => project.id === state.project);
const projectRuns = () => model.runs.filter(run => projectKey(run) === state.project).map(run => ({...run, color:customRunColor(run)}));
/** One project's analysis choices feed every run selector and plot.
 * Main callers: filteredRuns, analysis controls, filter dialog and metricPanel.
 */
function analysis() {
  if (!state.analysisPreferences.has(state.project)) state.analysisPreferences.set(state.project, {filters:[], sort:'run:created_at', direction:-1, groupBy:'', aggregation:'mean', range:'stddev'});
  return state.analysisPreferences.get(state.project);
}
const filteredRuns = () => {
  const choices = analysis();
  if (searchError(state.search, choices.searchRegex)) return [];
  const runs = selectAnalysisRuns(projectRuns(), {...choices,search:state.search,status:state.status,group:state.group});
  const pins = new Set(layout().pinnedRuns || []);
  const sorted = runs.sort((a,b) => Number(pins.has(b.key))-Number(pins.has(a.key)));
  return choices.groupBy ? groupRuns(sorted,choices.groupBy).flatMap(group=>group.runs) : sorted;
};
function layout() {
  if (!state.workspaceLayouts.has(state.project)) state.workspaceLayouts.set(state.project,newLayout());
  return state.workspaceLayouts.get(state.project);
}
let projectSourceCache=new Map();
let renderedCatalog=null;
/** Catalog discovery uses all project data, independent of eyes and filters.
 * Main callers: panel bank, Quick Add, saved panel actions. Cache discovery until
 * a new server snapshot; an empty filtered view must retain its panel identities.
 */
function panelCatalog() {
  if(renderedCatalog)return renderedCatalog;
  if(!projectSourceCache.has(state.project))projectSourceCache.set(state.project,discoverPanelSources(projectRuns()));
  const definitions=CUSTOM_WORKSPACES ? layout().panels || [] : [];
  const panels=materializePanels({runs:projectRuns(),sources:projectSourceCache.get(state.project),definitions,mode:CUSTOM_WORKSPACES ? layout().panelMode || 'automated' : 'automated'});
  renderedCatalog=new Map(panels.map(panel=>[panel.id.startsWith('auto:') && panel.type==='line'?panel.source:panel.id,panel]));
  for(const copy of CUSTOM_WORKSPACES ? layout().copies || [] : [])if(renderedCatalog.has(copy.metric))renderedCatalog.set(copy.id,{...renderedCatalog.get(copy.metric),id:copy.id});
  return renderedCatalog;
}
const sourceMetric=id=>state.panelSettings.get(panelKey(state.project,id))?.source || panelCatalog().get(id)?.source || id;
const allPanels=()=>[...panelCatalog().keys()];
const panelTitle=id=>state.panelSettings.get(panelKey(state.project,id))?.title || panelCatalog().get(id)?.title || sourceMetric(id);
const customRunColor = run => /^#[0-9a-f]{6}$/i.test(state.runColors.get(run.key) || '') ? state.runColors.get(run.key) : run.color;
// Checkboxes select records for bulk actions; eyes control plotting independently.
const selectedRuns = () => filteredRuns().filter(run => state.selected.has(run.key));
const visibleRuns = () => filteredRuns().filter(run => !state.hiddenRuns.has(run.key));
const pathNote = keys => keys.some(key => key.includes('\\')) ? '<p class="table-note path-note">Dots separate nested fields. A backslash escapes a literal dot or backslash in a logged key.</p>' : '';
const isDemo = () => /(^|[\/_-])(demo|synthetic|example)([\/_-]|$)/i.test(state.project) || projectRuns().some(run => (run.tags || []).includes('synthetic-demo'));
const compactDate = value => value ? new Date(value).toLocaleDateString([], { month: 'short', day: 'numeric' }) : 'Unknown';
const fullDate = value => value ? new Date(value).toLocaleString() : 'Not recorded';
const age = value => { const secs = Math.max(0, Math.round((Date.now() - new Date(value)) / 1000)); return secs < 60 ? `${secs}s ago` : secs < 3600 ? `${Math.floor(secs / 60)}m ago` : secs < 86400 ? `${Math.floor(secs / 3600)}h ago` : `${Math.floor(secs / 86400)}d ago`; };
const stateBadge = run => `<span class="status-${e(run.state)}"><span class="dot state-dot ${e(run.state)}"></span> ${e(run.state)}</span>`;
const button = (action, title, glyph, classes = '') => `<button class="icon-btn ${classes}" data-action="${e(action)}" title="${e(title)}" aria-label="${e(title)}">${icon(glyph)}</button>`;
const searchBox = (key, value, placeholder, extra = '') => `<div class="search ${extra}">${icon('search')}<input data-state="${key}" type="search" value="${e(value)}" placeholder="${e(placeholder)}" aria-label="${e(placeholder)}"></div>`;

function renderTopbar() {
  return `<header class="topbar"><a class="brand" href="./" aria-label="Backprop home"><img src="./favicon.svg" alt="">Backprop<span>Experiments</span></a><select ${state.loading ? 'disabled' : ''} data-state="project" class="project-mobile header-project" aria-label="Project">${state.loading ? '<option>Loading projects…</option>' : projectOptions()}</select><div class="topbar-right"><div class="connection"><span class="dot" style="background:${state.error ? 'var(--error)' : state.auth.authenticated ? 'var(--teal)' : 'var(--dim)'}"></span><span class="connection-label">${state.error ? 'Connection needs attention' : state.auth.authenticated ? 'Connected to Foundry' : 'Secure workspace'}</span></div>${state.auth.authenticated ? `<button class="avatar" data-action="logout" title="Sign out of Backprop" aria-label="Sign out of Backprop">${e((state.auth.user?.name || state.auth.user?.displayName || 'BP').slice(0, 2).toUpperCase())}</button>` : ''}</div></header>`;
}
function projectOptions() { return model.projects.length ? model.projects.map(project => `<option value="${e(project.id)}" ${state.project === project.id ? 'selected' : ''}>${e(project.name)}</option>`).join('') : '<option value="">Select a project</option>'; }
function renderSidebar() {
  const views = [['overview','info','Project'],['workspace', 'grid', 'Workspace'], ['runs', 'runs', 'Runs'], ['artifacts', 'data', 'Artifacts']];
  return `<aside class="sidebar"><div class="eyebrow project-label">Project</div><select class="project-select" data-state="project" aria-label="Choose project">${projectOptions()}</select><nav class="nav-group" aria-label="Project views">${views.map(([key, glyph, label]) => `<button class="nav-item ${(state.view === key || key === 'workspace' && state.view === 'run') ? 'active' : ''}" data-view="${key}" title="${label}" aria-label="${label}" ${(state.view === key || key === 'workspace' && state.view === 'run') ? 'aria-current="page"' : ''}>${icon(glyph)}<span class="nav-label">${label}</span>${key === 'runs' ? `<span class="count">${projectRuns().length}</span>` : ''}</button>`).join('')}</nav><nav class="nav-group"><button class="nav-item ${state.view === 'setup' ? 'active' : ''}" data-view="setup" title="Connect your code" aria-label="Connect your code">${icon('code')}<span class="nav-label">Connect your code</span></button></nav><div class="sidebar-bottom"><div class="eyebrow">Built on Foundry</div><p>Backprop · logging + visualization<br>30 second refresh interval</p></div></aside>`;
}

/** Keep stable controls focused when a state update replaces their DOM.
 * Main callers: workspace refresh/actions and Summary dialog interactions.
 * Mutable display attributes are excluded from identity; chart keys refer to
 * the original metric/value rather than the transient SVG registry number.
 */
function rememberFocus(root) {
  const active = document.activeElement;
  return root.contains(active) ? { tag: active.tagName, data: Object.entries(active.dataset).filter(([key]) => key !== 'visible'), start: active.selectionStart, end: active.selectionEnd,
    chart: active.matches('.chart-svg') ? chartRegistry.get(active.closest('[data-chart]')?.dataset.chart)?.key : null } : null;
}
function restoreFocus(root, focus) {
  if (focus) {
    const node = focus.chart ? [...root.querySelectorAll('.chart-svg')].find(el => chartRegistry.get(el.closest('[data-chart]').dataset.chart)?.key === focus.chart)
      : focus.data.length ? [...root.querySelectorAll('button,input,select,a,[tabindex]')].find(el => el.tagName === focus.tag && focus.data.every(([key, value]) => el.dataset[key] === value)) : null;
    if (node) { node.focus({ preventScroll: true }); if (node.type === 'search' || node.type === 'text') node.setSelectionRange(focus.start, focus.end); }
  }
}

/** Render from state, restoring focused inputs after updates.
 * Main callers: refresh and delegated interactions. This keeps polling from
 * stealing the caret while someone searches a large collection of runs.
 */
function render({restore = null} = {}) {
  refreshRenderer.cancel();
  const revision = ++renderRevision;
  const scope = JSON.stringify([state.project,state.view,state.runKey,state.runTab]);
  const viewState = restore || (renderedViewScope === scope ? captureViewState(app) : null);
  const focus = viewState ? null : rememberFocus(app);
  renderedViewScope = scope;
  renderedCatalog=null;
  hideControlTooltip();
  document.querySelector('.action-menu')?.remove();
  chartRegistry.clear();
  mediaRegistry.clear();
  plotlyRegistry.clear();
  vegaRegistry.clear();
  richRegistry.clear();
  tableRegistry.clear();
  tableContexts.clear();
  artifactTableRegistry.clear();
  state.auth = getAuthState();
  if (!state.auth.authenticated) {
    app.innerHTML = renderTopbar() + renderAuth();
    return;
  }
  if (state.loading) {
    replaceView(app, renderTopbar() + renderLoading());
    return;
  }
  replaceView(app, `${renderTopbar()}<div class="shell ${state.compactNavigation ? 'compact-navigation' : ''}">${renderSidebar()}<main class="main ${['workspace','runs','run'].includes(state.view)?'workbench-main':''}">${renderMain()}</main></div>`);
  bindCharts(app);
  bindWorkspaceLayout(app,{layout:layout(),columns:state.columns,onCommit:()=>persist(),
    onMove:(metric,section,before)=>{movePanel(layout(),allPanels(projectRuns()),metric,section,before);state.pinnedPanels.delete(panelKey(state.project,metric));persist();render();},
    onSectionMove:(section,before)=>{const ids=arrangePanels(allPanels(projectRuns()),layout(),new Set(),sourceMetric).map(item=>item.id).filter(id=>id!==section);ids.splice(Math.max(0,ids.indexOf(before)),0,section);layout().sectionOrder=ids;persist();render();}});
  app.querySelector('.workspace-settings')?.addEventListener('toggle',event=>{layout().settingsOpen=event.target.open;persist();});
  const hydration = hydrateMedia(app);
  if (viewState) {
    const restoreScroll = restoreViewState(app, viewState);
    hydration.then(() => { if (revision === renderRevision) restoreScroll(); });
  } else restoreFocus(app, focus);
}

/** Initial data read: show an honest waiting state without empty experiment UI.
 * Main callers: render, after Foundry authentication and before the first read.
 * Project names come from the link and are escaped, never treated as markup.
 */
function renderLoading() {
  const project = new URLSearchParams(location.search).get('project') || '';
  return `<main class="workspace-loading" aria-busy="true"><section class="loading-card" role="status" aria-live="polite"><div class="loading-brand"><img src="./favicon.svg" alt=""><span>BACKPROP <span>/ EXPERIMENTS</span></span></div><h1>Opening your project</h1><p>Your runs, charts, and logged data in one place.</p>${project ? `<div class="loading-project">${icon('grid')}<span>${e(project)}</span></div>` : ''}<div class="loading-footer"><span class="loading-spinner" aria-hidden="true"></span><div><p class="loading-status">Loading experiments…</p><p class="loading-note">Large histories can take a little longer.</p></div></div></section></main>`;
}

function renderAuth() {
  return `<main class="auth-screen"><div class="auth-card"><img src="./favicon.svg" alt="Backprop"><div class="eyebrow">Your experiments, in focus</div><h1>See what learns.</h1><p>Track training runs, compare metrics, and inspect every logged result in one workspace connected to Palantir Foundry.</p>${state.error ? `<div class="notice error">${icon('info')}<span>${e(state.error)}</span></div><button class="btn" data-action="refresh">Retry connection</button>` : ''}<button class="btn primary" data-action="login" ${state.loading ? 'disabled' : ''}>${icon('lock')} ${state.loading ? 'Connecting...' : 'Continue with Foundry'} ${icon('arrow')}</button><p class="sub">Use your Foundry account to access experiment data.<br>Access follows your existing Foundry permissions.</p><div class="auth-trace"><span>${icon('pulse')} Live metrics</span><span>${icon('compare')} Run comparison</span><span>${icon('data')} Rich logged data</span></div></div></main>`;
}
function renderMain() {
  const project = chosenProject(), runs = projectRuns(), selected = selectedRuns();
  const title = { overview:'Project',artifacts:'Artifacts',workspace: 'Workspace', runs: 'Runs', compare: 'Compare runs', data: 'Logged data', setup: 'Connect your code' }[state.view] || 'Workspace';
  const subtitles = {overview:'Project activity and recorded experiments.',artifacts:'Versioned data and models used by your experiments.', workspace: `${runs.length} runs · ${runs.filter(run => run.state === 'running').length} running · ${metricKeys(runs).length} metrics`, runs: 'Explore every experiment and its latest results.', compare: 'Find what changed, and what made the difference.', data: 'Inspect the tables, distributions, plots, and media behind your metrics.', setup: 'Keep your logging workflow. Bring your experiments into Foundry.' };
  const intro = `<div class="breadcrumb">${icon('grid')}<span>${e(project?.entity || 'Foundry')}</span><span>/</span><strong>${e(project?.name || 'Experiments')}</strong><span>/</span><span>${e(title)}</span></div><div class="page-heading"><div><h1>${e(title)}</h1><p>${subtitles[state.view] || subtitles.workspace}</p></div><div class="heading-actions">${isDemo() ? '<span class="badge demo">Synthetic demo</span>' : ''}<button class="btn ghost live-btn ${state.live ? 'active' : ''}" data-action="live"><span class="dot"></span>${state.live ? 'Live · 30s' : 'Updates paused'}</button><button class="btn" data-action="refresh" ${state.refreshing ? 'disabled' : ''}>${icon('refresh')} ${state.refreshing ? 'Refreshing' : 'Refresh'}</button></div></div>`;
  const projectIssues = [...(model.globalIssues || []), ...(model.issuesByProject?.[state.project] || [])];
  const warning = `${state.error ? `<div class="notice error">${icon('info')}<span>${e(state.error)}${state.fetchedAt ? ` Showing data last loaded ${e(age(state.fetchedAt))}.` : ''}</span><button class="btn small" data-action="refresh">Retry</button></div>` : ''}${preferenceError ? `<div class="notice error" role="alert">${e(preferenceError)}</div>` : ''}${state.warning ? `<div class="notice">${icon('info')}<span>${e(state.warning)}</span></div>` : ''}${projectIssues.length ? `<div class="notice">${icon('info')}<span>${e(projectIssues.join(' '))}</span></div>` : ''}`;
  if (state.view === 'setup') return intro + warning + renderSetup();
  if(state.view==='overview' && project)return intro+warning+renderProjectOverview({project,runs,icon,date:fullDate});
  if(state.view==='artifacts')return intro+warning+renderProjectArtifacts({runs,selection:state.artifactSelection,search:state.artifactSearch,icon,date:fullDate,renderFiles:renderRunFiles});
  if (!model.runs.length) return intro + warning + `<div class="empty">${icon('pulse')}<h2>Your next experiment starts here.</h2><p>Connect the Backprop Python SDK and log your first run. Metrics will appear here as they arrive in Foundry.</p><button class="btn primary" data-view="setup">${icon('code')} Connect your code</button><p>Already logging? Use Refresh to load the latest data.</p></div>`;

  if (state.view === 'run') return renderWorkspaceHeading() + warning + renderRunPage();
  let content = state.view === 'runs' ? renderRuns() : state.view === 'compare' ? renderCompare() : state.view === 'data' ? renderData() : renderWorkspace();
  if(['workspace','runs'].includes(state.view))return renderWorkspaceHeading()+warning+content;
  return intro + warning + content + `<footer class="workspace-footer"><span>${icon('lock')} Data stored in Palantir Foundry${isDemo() ? ' · Synthetic example data' : ''}</span><span>${state.fetchedAt ? `Loaded ${e(new Date(state.fetchedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }))}` : ''} · ${e(state.source || 'Foundry stream')}</span></footer>`;
}

function analysisButtons() {
  const choices = analysis();
  return `<div class="analysis-buttons"><button class="btn small ${choices.filters.length ? 'active' : ''}" data-action="run-filters">Filter${choices.filters.length ? ` · ${choices.filters.length}` : ''}</button><button class="btn small ${choices.groupBy ? 'active' : ''}" data-action="run-grouping">Group</button><button class="btn small" data-action="run-sort">Sort</button><button class="btn small" data-action="run-columns">Columns</button></div>`;
}
function colorControl(run) {
  return `<input class="run-color" type="color" data-run-color="${e(run.key)}" value="${e(run.color)}" aria-label="Color for ${e(run.name)}" title="Change run color">`;
}

/** Search modes share matching and validation across sidebar and full table.
 * Main callers: both run selectors. Invalid patterns show an error and no rows.
 */
function searchError(text, regex) { try { if(regex) new RegExp(text,'i'); return ''; } catch(error) { return `Invalid regular expression: ${error.message}`; } }
function runSearch() {
  return `<div class="run-search">${searchBox('search', state.search, 'Find runs', 'wide')}<button class="icon-btn ${analysis().searchRegex?'active':''}" data-action="run-regex" aria-label="Use regular expression for run search" aria-pressed="${!!analysis().searchRegex}" data-tooltip="Regular expression">.*</button></div>${searchError(state.search,analysis().searchRegex)?`<p class="notice error" role="alert">${e(searchError(state.search,analysis().searchRegex))}</p>`:''}`;
}
function runPagination(total) {
  const size=layout().runPageSize || 20, pages=Math.max(1,Math.ceil(total/size));
  state.runPage=Math.min(state.runPage,pages-1);
  return `<div class="table-pagination run-pagination"><label>Rows<select data-run-page-size aria-label="Runs per page">${[20,50,100].map(count=>`<option ${size===count?'selected':''}>${count}</option>`).join('')}</select></label><span>${total?state.runPage*size+1:0}-${Math.min((state.runPage+1)*size,total)} of ${total}</span><span><button class="icon-btn" data-page="-1" aria-label="Previous runs page" ${state.runPage===0?'disabled':''}>${icon('left')}</button><button class="icon-btn" data-page="1" aria-label="Next runs page" ${state.runPage+1>=pages?'disabled':''}>${icon('chevron')}</button></span></div>`;
}
/** Eyes control chart inclusion; row checkboxes appear only in the full table.
 * Main callers: workspace and rich-output canvases. Pagination never limits the
 * collection passed to charts, and filtering never discards visibility choices.
 */
function visibilityControl(run) {
  const visible=!state.hiddenRuns.has(run.key);
  return `<button class="icon-btn run-visibility ${visible?'':'run-hidden'}" data-run-visibility="${e(run.key)}" aria-pressed="${visible}" aria-label="${visible?'Hide':'Show'} run ${e(run.name)}" data-tooltip="${visible?'Hide':'Show'} run">${icon('eye')}${visible?'':'<span class="eye-slash" aria-hidden="true"></span>'}</button>`;
}
function visibilityMenu() {
  return `<button class="icon-btn" data-action="visibility-menu" aria-label="Run visibility options" aria-haspopup="menu">${icon('eye')}</button>`;
}
function renderRunPanel() {
  const all=filteredRuns(),pagination=runPagination(all.length),size=layout().runPageSize || 20;
  const runs=all.slice(state.runPage*size,(state.runPage+1)*size),pins=new Set(layout().pinnedRuns || []);
  const columns=runColumns().filter(([key])=>(layout().pinnedColumns || []).includes(key));
  const rows=groupRuns(runs,analysis().groupBy).map(group=>`
    ${analysis().groupBy?`<div class="sidebar-group"><span>${e(group.name)}</span><span>${group.runs.length} on page</span></div>`:''}
    ${group.runs.map(run=>`<div data-view-key="${e(run.key)}" class="run-item ${state.hiddenRuns.has(run.key)?'muted-run':''}">
      ${visibilityControl(run)}${colorControl(run)}
      <div class="run-meta"><button class="run-name" data-run="${e(run.key)}" title="${e(run.name)}">${e(run.name)}</button>
      </div>
      <span class="dot state-dot ${e(run.state)}" title="${e(run.state)}"></span>
      <button class="icon-btn run-pin ${pins.has(run.key)?'active':''}" data-pin-run="${e(run.key)}" aria-pressed="${pins.has(run.key)}" aria-label="${pins.has(run.key)?'Unpin':'Pin'} run ${e(run.name)}">${icon('pin')}</button>${columns.map(([key,label])=>`<div class="pinned-run-value" data-pinned-field="${e(key)}" title="${e(label)}">${runColumnValue(run,key)}</div>`).join('')}
    </div>`).join('')}`).join('');
  return `<aside class="run-panel" aria-label="Runs sidebar" style="--pinned-columns:${columns.length};--run-grid:24px 18px minmax(90px,1fr) 8px 23px ${columns.map(()=>'130px').join(' ')}"><div class="run-panel-header">
    <div class="run-panel-title">Runs <span class="count">${projectRuns().length}</span><span class="grow"></span><button class="icon-btn" data-action="hide-run-sidebar" aria-label="Hide runs sidebar">${icon('left')}</button><button class="icon-btn" data-action="expand-run-table" aria-label="Expand full runs table">${icon('expand')}</button></div>
    ${runSearch()}${analysisButtons()}</div>
    <div class="run-list"><div class="run-list-columns">${visibilityMenu()}<span class="run-name-heading">Name <span class="count">${all.length} listed</span></span>${columns.map(([key,label])=>`<button data-sort="${e(key)}" title="Sort by ${e(label)}">${e(label)}</button>`).join('')}</div>${rows || '<div class="chart-empty">No runs match these filters.</div>'}</div>${pagination}</aside>`;
}

/** Render one section hierarchy for project and individual-run canvases.
 * Main callers: workspace and run Charts tab. Saved order/defaults stay shared;
 * only the run collection changes, so panel controls remain consistent.
 */
function renderMetricSections(shown,runs) {
  const pinned = new Set(shown.filter(metric=>state.pinnedPanels.has(panelKey(state.project,metric))));
  const sections = arrangePanels(shown,layout(),pinned,sourceMetric);
  return sections.map(section => {
    const collapsed = state.collapsedSections.has(panelKey(state.project,section.id));
    const size=section.pageSize || 6, page=Math.min(section.page || 0,Math.max(0,Math.ceil(section.metrics.length/size)-1));
    if(page!==(section.page || 0))setSection(layout(),section.id,{page});
    const controls=section.metrics.length>size?`<span class="section-pages">${page*size+1}-${Math.min((page+1)*size,section.metrics.length)} of ${section.metrics.length}<button class="icon-btn" data-section-page="${e(section.id)}" data-page-delta="-1" aria-label="Previous panels in ${e(section.title)}" ${page===0?'disabled':''}>${icon('left')}</button><button class="icon-btn" data-section-page="${e(section.id)}" data-page-delta="1" aria-label="Next panels in ${e(section.title)}" ${(page+1)*size>=section.metrics.length?'disabled':''}>${icon('chevron')}</button></span>`:'';
    return `<section class="metric-section" data-section-id="${e(section.id)}"><div class="section-heading-row">${CUSTOM_WORKSPACES?`<button class="panel-drag" draggable="true" data-section-drag="${e(section.id)}" aria-label="Drag ${e(section.title)} section">⠿</button>`:''}<button class="section-heading section-toggle ${collapsed ? 'collapsed' : ''}" data-section="${e(section.id)}" aria-expanded="${!collapsed}">${icon('chevron')} ${e(section.title)} <span>${section.metrics.length} panels</span><span class="line"></span></button>${collapsed?'':controls}${CUSTOM_WORKSPACES?`<button class="icon-btn ${section.pinned?'active':''}" data-section-pin="${e(section.id)}" aria-pressed="${!!section.pinned}" aria-label="${section.pinned?'Unpin':'Pin'} ${e(section.title)} section">${icon('pin')}</button><button class="icon-btn" data-section-settings="${e(section.id)}" aria-label="Settings for ${e(section.title)} section">${icon('settings')}</button><button class="icon-btn" data-section-menu="${e(section.id)}" aria-label="Actions for ${e(section.title)} section" aria-haspopup="menu">⋯</button>`:''}</div>${collapsed ? '' : `<div class="chart-grid cols-${state.columns}">${section.metrics.slice(page*size,(page+1)*size).map(key=>renderMetricCard(key,runs)).join('') || `<div class="empty-section"><button class="btn small" data-section-add-panel="${e(section.id)}">Add panels</button></div>`}</div>`}</section>`;
  }).join('');
}

function renderWorkspace(singleRun=null) {
  const hideSidebar=Boolean(singleRun) || layout().sidebarHidden;
  const runs = singleRun || visibleRuns(), metrics = allPanels().filter(key=>CUSTOM_WORKSPACES || !singleRun || panelCatalog().get(key).runKeys?.some(runKey=>singleRun.some(run=>run.key===runKey))), panelError=searchError(state.metricSearch,state.metricRegex), pattern=!panelError && state.metricRegex?new RegExp(state.metricSearch,'i'):null, shown = metrics.filter(key => !panelError && (pattern?pattern.test(panelTitle(key)):panelTitle(key).toLowerCase().includes(state.metricSearch.toLowerCase())) && !state.hiddenPanels.has(panelKey(state.project, key)));
  const toolbar = `<div class="toolbar workspace-toolbar">${!singleRun && hideSidebar?`<button class="btn small" data-action="show-run-sidebar">${icon('runs')} Show runs</button>`:''}${searchBox('metricSearch', state.metricSearch, 'Search panels')}<button class="icon-btn ${state.metricRegex?'active':''}" data-action="panel-regex" aria-label="Use regular expression for panel search" aria-pressed="${!!state.metricRegex}">.*</button>${CUSTOM_WORKSPACES?`<button class="icon-btn" data-action="workspace-settings" aria-label="Workspace settings" data-tooltip="Workspace settings">${icon('settings')}</button><button class="btn primary" data-action="add-panels">+ Add panels</button><button class="icon-btn" data-action="manage-panels" aria-label="Panel bank actions" aria-haspopup="dialog">⋯</button>`:''}</div>`;
  let panels = renderMetricSections(shown,runs);
  if(shown.length && !panels)panels='<div class="empty"><h2>All sections are hidden.</h2><p>Restore your sections and panels from the panel manager.</p><button class="btn" data-action="manage-panels">Choose panels</button></div>';
  if (!runs.length && !shown.length) panels = `<div class="empty">${icon('compare')}<h2>No visible runs.</h2><p>Use the eyes in the run list to show experiments in your panels.</p><button class="btn" data-action="show-all-runs">Show matching runs</button></div>`;
  else if (metrics.length && !state.metricSearch && metrics.every(key => state.hiddenPanels.has(panelKey(state.project, key)))) panels = `<div class="empty">${icon('grid')}<h2>All panels are hidden.</h2><p>Your metrics and saved settings are still here. Choose which panels to show.</p><button class="btn" data-action="manage-panels">Choose panels</button></div>`;
  else if (!shown.length) panels = `<div class="empty">${icon('pulse')}<h2>${metrics.length ? 'No matching metrics.' : 'Waiting for metrics.'}</h2><p>${metrics.length ? 'Try a different search or clear the metric filter.' : 'Metrics, tables, plots and media appear here automatically as they are logged.'}</p>${metrics.length ? '<button class="btn" data-action="clear-metrics">Clear search</button>' : ''}</div>`;
  return `<div class="workspace layout-workspace ${hideSidebar?'sidebar-hidden':''}" style="--run-width:${layout().sidebarWidth}px">${hideSidebar?'':renderRunPanel()}${hideSidebar?'':`<div class="sidebar-resizer" data-sidebar-resize role="separator" tabindex="0" aria-label="Resize run sidebar" aria-orientation="vertical" aria-valuemin="190" aria-valuemax="540" aria-valuenow="${layout().sidebarWidth}"></div>`}<div class="charts-area">${toolbar}${panelError?`<p class="notice error" role="alert">${e(panelError)}</p>`:''}${pathNote(metrics)}${panels}${CUSTOM_WORKSPACES?'<button class="btn small add-section-bottom" data-action="add-section">+ Add section</button>':''}</div></div>`;
}

/** One saved workspace identity stays visible across table, panels and run detail.
 * Main callers: renderMain. Saved means the stream acknowledged the snapshot;
 * pending browser changes never receive that label.
 */
function renderWorkspaceHeading() {
  if(!CUSTOM_WORKSPACES)return `<div class="workspace-chrome"><header class="workspace-heading" data-workspace-default><div class="workspace-identity"><h1>Workspace</h1><span class="workspace-project">${e(chosenProject()?.name || '')}</span></div><div class="workspace-header-actions"><span class="workspace-local-note" data-tooltip="Filters, visibility and browsing choices are remembered in this browser">This browser</span>${isDemo()?'<span class="badge demo">Synthetic demo</span>':''}<button class="btn small live-btn ${state.live?'active':''}" data-action="live" aria-pressed="${state.live}" aria-label="${state.live?'Pause live updates':'Resume live updates'}" data-tooltip="${state.live?'Live updates every 30 seconds':'Updates paused'}"><span class="dot"></span>${state.live?'Live · 30s':'Paused'}</button><button class="icon-btn" data-action="refresh" aria-label="Refresh experiments" ${state.refreshing?'disabled':''}>${icon('refresh')}</button></div></header></div>`;
  const status=workspaceSession?.status(),name=status?.name || 'Personal workspace';
  return `<div class="workspace-chrome"><header class="workspace-heading"><div class="workspace-identity">${savedViewControls()}<h1>${e(name)}</h1><span class="badge">${status?.autoSave?'Personal workspace':'Saved view'}</span></div><div class="workspace-header-actions">${isDemo()?'<span class="badge demo">Synthetic demo</span>':''}<span data-workspace-status role="status" class="workspace-save-status ${status?.error||status?.conflict?'error':''}" title="${e(status?.error || status?.updatedAt || '')}">${e(workspaceSaveLabel(status,getAuthState().canSaveWorkspaces))}</span>${status?.dirty&&!status.autoSave?'<button class="btn small" data-action="clear-view-changes">Clear changes</button>':''}<button class="icon-btn" data-action="undo-view" aria-label="Undo last action" ${status?.canUndo?'':'disabled'}>${icon('reset')}</button><button class="icon-btn redo-button" data-action="redo-view" aria-label="Redo last action" ${status?.canRedo?'':'disabled'}>${icon('reset')}</button>${status&&!status.autoSave?`<button class="btn small" data-action="save-view" ${status.dirty||status.error?'':'disabled'}>Save view</button>`:''}<button class="btn small" data-action="save-view-as" ${status?.loaded?'':'disabled'}>Save as</button><button class="icon-btn" data-action="workspace-menu" aria-label="Workspace actions" aria-haspopup="menu">⋯</button><button class="icon-btn ${state.live?'active':''}" data-action="live" aria-pressed="${state.live}" aria-label="${state.live?'Pause live updates':'Resume live updates'}" data-tooltip="${state.live?'Live updates every 30 seconds':'Updates paused'}">${icon('pulse')}</button><button class="icon-btn" data-action="refresh" aria-label="Refresh experiments" ${state.refreshing?'disabled':''}>${icon('refresh')}</button></div></header>${workspaceSessionError?`<div class="notice error" role="alert">${e(workspaceSessionError)}<button class="btn small" data-action="reload-view">Retry workspace</button></div>`:''}${status?.error||status?.conflict?`<div class="notice error" role="alert">${e(status.error || 'This view has conflicting saved versions. Choose which changes to keep.')}<button class="btn small" data-action="save-view">Retry save</button>${status.conflict?'<button class="btn small" data-action="resolve-view">Keep my changes</button><button class="btn small" data-action="adopt-saved-view">Use latest saved version</button>':''}</div>`:''}${status&&!getAuthState().canSaveWorkspaces?'<div class="workspace-save-permission"><span>Workspace changes are not being saved to your account.</span><button class="btn small" data-action="enable-workspace-save">Enable saving</button></div>':''}</div>`;
}
function savedViewControls() {
  const views=workspaceSession?.views() || [],current=workspaceSession?.status(),active=current?.scope.viewId || 'personal';
  if(current&&!current.autoSave&&!views.some(view=>view.scope.viewId===active))views.push({scope:current.scope,name:current.name});
  return `<label class="saved-view-select">${icon('grid')}<select data-view-choice aria-label="Saved workspace view"><option value="personal">Personal workspace</option>${views.filter(view=>view.scope.viewId!=='personal').map(view=>`<option value="${e(view.scope.viewId)}" ${view.scope.viewId===active?'selected':''}>${e(view.name)}${view.localDraft?' (recovered draft)':''}</option>`).join('')}</select></label>`;
}
/** Save/clone display choices through authenticated Foundry writes.
 * Main callers: workspace Save and Save as. Named views are available to people
 * with access to the existing project dataset. This does not alter permissions.
 */
async function saveView(asNew) {
  if(!workspaceSession)throw new Error('Wait for workspace settings to load.');
  if(!getAuthState().canSaveWorkspaces){enableWorkspaceSaving();return;}
  if(!asNew){await workspaceSession.save();toast('Workspace saved.');return;}
  inspectDialog.className='small-dialog';
  inspectDialog.innerHTML=`<form data-save-view-form><header class="dialog-header"><h2>Save workspace view</h2><button type="button" class="icon-btn" data-action="close-dialog" aria-label="Cancel saving view">${icon('close')}</button></header><div class="dialog-body analysis-settings"><label>View name<input name="name" maxlength="80" required placeholder="Learning-rate comparison"></label><p class="dim">Save filters, run visibility, colors, panels and chart settings. This view is available to people with access to this project's data.</p><p class="filter-error" role="alert" hidden></p></div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button class="btn primary" type="submit">Save view</button></footer></form>`;
  inspectDialog.querySelector('form').addEventListener('submit',async event=>{event.preventDefault();const submit=event.currentTarget.querySelector('[type="submit"]');submit.disabled=true;try{await workspaceSession.saveAs(new FormData(event.currentTarget).get('name'));updateLink();inspectDialog.close();render();toast('Workspace view saved.');}catch(error){const notice=inspectDialog.querySelector('[role="alert"]');notice.textContent=error.message;notice.hidden=false;}finally{submit.disabled=false;}});
  inspectDialog.showModal();
}
function enableWorkspaceSaving(){
  inspectDialog.className='small-dialog';inspectDialog.innerHTML=`<header class="dialog-header"><h2>Enable workspace saving</h2><button class="icon-btn" data-action="close-dialog" aria-label="Cancel">${icon('close')}</button></header><div class="dialog-body"><p>Allow Backprop to write workspace settings to the existing experiment stream. Your personal workspace will autosave; named views save when you choose Save view.</p><p class="dim">Foundry will ask you to approve stream-write access. Access to project data remains governed by your existing permissions.</p></div><footer class="dialog-footer"><button class="btn" data-action="close-dialog">Cancel</button><button class="btn primary" data-action="confirm-workspace-save">Continue to Foundry</button></footer>`;inspectDialog.showModal();
}
/** Display-only section/panel actions reuse layout and chart rendering.
 * Main callers: overflow menus. Removing a panel never removes logged data;
 * hidden automatic panels and sections can be restored from the panel manager.
 */
function sectionMenu(anchor,id) {
  const section=sectionOptions(layout(),id), ids=arrangePanels(allPanels(projectRuns()),layout(),new Set(),sourceMetric).map(item=>item.id);
  const commit=change=>{change();persist();render();};
  openActionMenu(anchor,[
    {label:'Add panels',run:()=>addPanelToSection(id)},
    {label:'Rename / section settings',run:()=>openSectionSettings(id)},
    {label:'Sort panels A–Z',run:()=>commit(()=>{const metrics=arrangePanels(allPanels(projectRuns()),layout(),new Set(),sourceMetric).find(item=>item.id===id)?.metrics || [];layout().panelOrder=[...metrics.sort((a,b)=>panelTitle(a).localeCompare(panelTitle(b))),...layout().panelOrder.filter(key=>!metrics.includes(key))];})},
    {label:'New section above',run:()=>openSectionSettings(`custom:${crypto.randomUUID()}`,true,id)},
    {label:'New section below',run:()=>openSectionSettings(`custom:${crypto.randomUUID()}`,true,ids[ids.indexOf(id)+1] || '')},
    null,{label:'Hide section',run:()=>commit(()=>setSection(layout(),id,{hidden:true}))},
  ],toast);
}
function addPanelToSection(section) { openPanelLibrary(section); }

function panelMenu(anchor,metric) {
  const pinned=state.pinnedPanels.has(panelKey(state.project,metric));
  openActionMenu(anchor,[
    {label:'Copy panel URL',run:async()=>{const url=new URL(location.href);url.searchParams.set('panel',metric);await navigator.clipboard.writeText(url.href);toast(CUSTOM_WORKSPACES?'Panel link copied. Save this view to share its latest settings.':'Panel link copied.');}},
    ...(panelCatalog().get(metric)?.type==='line'?[{label:'Download data as CSV',run:()=>exportMetric(metric)},
    {label:'Download chart as SVG',run:()=>{const {spec}=metricPanel(metric,chartRuns());if(!spec)throw new Error('No chart data to export.');const doc=new DOMParser().parseFromString(renderChart(spec,'export'),'text/html'),svg=doc.querySelector('svg');svg.setAttribute('xmlns','http://www.w3.org/2000/svg');svg.insertAdjacentHTML('afterbegin','<style>text{fill:#aab4c2;font:10px monospace}.grid-line{stroke:#404854}.crosshair,.zoom-selection{display:none}</style>');download(svg.outerHTML,`${fileName(panelTitle(metric))}.svg`,'image/svg+xml');}}]:[]),
    ...(CUSTOM_WORKSPACES ? [
    null,{label:'Move / resize panel',run:()=>openPanelLayout(metric)},
    {label:pinned?'Unpin panel':'Pin panel',run:()=>{const key=panelKey(state.project,metric);pinned?state.pinnedPanels.delete(key):state.pinnedPanels.add(key);persist();render();}},
    {label:'Duplicate panel',run:()=>{const original=panelCatalog().get(metric),panel=createPanel({...original,id:undefined,title:`${panelTitle(metric)} (copy)`});layout().panels ||=[];if(!layout().panels.some(item=>item.id===original.id))layout().panels.push(createPanel(original));layout().panels.push(panel);setPanelOptions(layout(),panel.id,{...panelOptions(layout(),metric),section:metricSection(metric)});state.panelSettings.set(panelKey(state.project,panel.id),{...state.panelSettings.get(panelKey(state.project,metric)),title:panel.title});setSection(layout(),metricSection(metric),{page:0});renderedCatalog=null;persist();render();}},
    null,{label:'Remove panel from view',run:()=>{state.hiddenPanels.add(panelKey(state.project,metric));persist();render();if(chartDialog.open)chartDialog.close();toast('Panel hidden. Restore it from Panels; logged data is unchanged.');}},
    ] : []),
  ],toast);
}

/** Provide keyboard-accessible movement and sizing alongside drag handles.
 * Main callers: panel Layout button. Metric identity is preserved across moves.
 */
function openPanelLayout(metric) {
  const options=panelOptions(layout(),metric), sections=arrangePanels(allPanels(projectRuns()),layout(),new Set(),sourceMetric), current=options.section || defaultSection(sourceMetric(metric));
  inspectDialog.className='small-dialog';
  inspectDialog.innerHTML=`<form><header class="dialog-header"><h2>Panel layout</h2><button type="button" class="icon-btn" data-action="close-dialog" aria-label="Cancel panel layout">${icon('close')}</button></header><div class="dialog-body analysis-settings"><label>Section<select name="section" aria-label="Section">${sections.map(section=>`<option value="${e(section.id)}" ${section.id===current?'selected':''}>${e(section.title)}</option>`).join('')}</select></label><label>Position<select name="before" aria-label="Position"><option value="">Last in section</option>${allPanels(projectRuns()).filter(key=>key!==metric).map(key=>`<option value="${e(key)}">Before ${e(key)}</option>`).join('')}</select></label><label>Panel width<select name="span" aria-label="Panel width">${[1,2,3].map(span=>`<option value="${span}" ${span===(options.span || 1)?'selected':''}>${span} column${span>1?'s':''}</option>`).join('')}</select></label><label>Panel height<input name="height" type="number" required min="230" max="700" value="${options.height || 270}"></label></div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button class="btn primary">Apply layout</button></footer></form>`;
  inspectDialog.querySelector('form').addEventListener('submit',event=>{event.preventDefault();const values=new FormData(event.currentTarget);movePanel(layout(),allPanels(projectRuns()),metric,values.get('section'),values.get('before'));setPanelOptions(layout(),metric,{span:Number(values.get('span')),height:Number(values.get('height'))});state.pinnedPanels.delete(panelKey(state.project,metric));persist();render();inspectDialog.close();});
  inspectDialog.showModal();
}
/** Edit section identity, order and inherited chart defaults in one form.
 * Main callers: Add section and section settings buttons. Renaming changes only
 * the display title; stable section IDs keep panel assignments intact.
 */
function openSectionSettings(id, isNew=false, beforeId=null) {
  const section=sectionOptions(layout(),id), settings=section.settings || {}, ids=arrangePanels(allPanels(projectRuns()),layout(),new Set(),sourceMetric).map(item=>item.id);
  inspectDialog.className='small-dialog';
  inspectDialog.innerHTML=`<form><header class="dialog-header"><h2>${isNew?'Add section':'Section settings'}</h2><button type="button" class="icon-btn" data-action="close-dialog" aria-label="Cancel section settings">${icon('close')}</button></header><div class="dialog-body analysis-settings"><label>Section name<input name="title" required maxlength="80" value="${isNew?'':e(section.title)}"></label><label>Position<select name="before" aria-label="Position"><option value="">Last section</option>${ids.filter(key=>key!==id).map(key=>`<option value="${e(key)}" ${(beforeId || ids[ids.indexOf(id)+1])===key ? "selected" : ""}>Before ${e(sectionOptions(layout(),key).title)}</option>`).join('')}</select></label>${chartDefaultsFields(settings,chartAxes())}<p class="dim">Panel overrides take priority over section defaults; section defaults take priority over workspace settings.</p><p data-scope-error role="alert" hidden></p></div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button class="btn primary">Apply section</button></footer></form>`;
  const sectionTitle=inspectDialog.querySelector('[name="title"]');sectionTitle.addEventListener('input',()=>sectionTitle.setCustomValidity(sectionTitle.value.trim() ? '' : 'Enter a section name.'));
  inspectDialog.querySelector('form').addEventListener('submit',event=>{event.preventDefault();try{const values=new FormData(event.currentTarget),defaults=readChartDefaults(event.currentTarget,workspaceDefaults());setSection(layout(),id,{title:values.get('title').trim(),custom:isNew || section.custom,settings:defaults});const order=ids.filter(key=>key!==id),before=order.indexOf(values.get('before'));before<0?order.push(id):order.splice(before,0,id);layout().sectionOrder=order;persist();render();inspectDialog.close();}catch(error){const node=inspectDialog.querySelector('[data-scope-error]');node.textContent=error.message;node.hidden=false;}});
  inspectDialog.showModal();
}

/** Workspace settings are one project/view scope; sections and panels inherit.
 * Main callers: settings dialog, panel editor and chart rendering.
 */
function workspaceDefaults(){return {...state,...(layout().chartSettings || {})};}
function chartAxes(){return [['defined','Defined step'],['_step','Step'],['_runtime','Relative time'],['_timestamp','Wall time'],...metricKeys(projectRuns(),true).map(key=>[key,key])];}
function openWorkspaceSettings(){
  inspectDialog.className='small-dialog';
  inspectDialog.innerHTML=`<form><header class="dialog-header"><h2>Workspace settings</h2><button type="button" class="icon-btn" data-action="close-dialog" aria-label="Cancel workspace settings">${icon('close')}</button></header><div class="dialog-body analysis-settings"><label>Panel creation<select name="panelMode"><option value="automated" ${layout().panelMode!=='manual'?'selected':''}>Automated</option><option value="manual" ${layout().panelMode==='manual'?'selected':''}>Manual</option></select></label><p class="dim">Automated discovers panels for new logged keys. Manual keeps only panels you add.</p><label>Chart columns<select name="columns">${[1,2,3].map(count=>`<option ${state.columns===count?'selected':''}>${count}</option>`).join('')}</select></label>${chartDefaultsFields(workspaceDefaults(),chartAxes(),'Application default')}<p class="dim">These defaults apply to this workspace. Section and panel overrides take priority.</p><p data-scope-error role="alert" hidden></p></div><footer class="dialog-footer"><button type="button" class="btn" data-action="close-dialog">Cancel</button><button class="btn primary">Apply workspace settings</button></footer></form>`;
  inspectDialog.querySelector('form').addEventListener('submit',event=>{event.preventDefault();try{const form=event.currentTarget,values=new FormData(form),defaults=readChartDefaults(form,defaultView(state.project));for(const key of ['xAxis','smoothing','smoothingMethod','logScale']){state[key]=defaults[key]??defaultView(state.project)[key];delete defaults[key];}layout().chartSettings=defaults;layout().panelMode=values.get('panelMode');state.columns=Number(values.get('columns'));state.zoom.clear();renderedCatalog=null;persist();render();inspectDialog.close();}catch(error){const notice=inspectDialog.querySelector('[data-scope-error]');notice.textContent=error.message;notice.hidden=false;}});
  inspectDialog.showModal();
}

/** Build one panel consistently for its preview, workspace and export.
 * Main callers: renderMetricCard, openPanel and exportMetric. Per-panel display
 * preferences never change global run selection or underlying recorded values.
 */
const metricSection = metric => panelOptions(layout(),metric).section || defaultSection(sourceMetric(metric));
function metricPanel(metric, runs, override = null) {
  const section = metricSection(metric);
  const settings = resolvePanel({...workspaceDefaults(),...sectionOptions(layout(),section).settings}, override ?? state.panelSettings.get(panelKey(state.project, metric)));
  const ys = [...new Set([settings.yMetric || sourceMetric(metric), settings.secondMetric].filter(Boolean))];
  const hidden = state.hiddenSeries.get(panelKey(state.project, metric)) || new Set();
  const inheritedChoices=analysis(),choices={...inheritedChoices};
  for(const key of ['groupBy','aggregation','range'])if(settings[key]!==undefined && settings[key]!=='inherit')choices[key]=settings[key];
  if(state.view==='run' && settings.groupBy===undefined)choices.groupBy='';
  const matchingRuns = runs.filter(run=>ys.some(key=>seriesFor(run,key,settings).length));
  const maxRuns=settings.maxRuns || 10;
  const plottedRuns = choices.groupBy ? matchingRuns : matchingRuns.slice(0,maxRuns).flatMap(splitRunWriters);
  const groups = groupRuns(plottedRuns, choices.groupBy).slice(0,choices.groupBy?maxRuns:Infinity);
  let all, error='';
  try { all = groups.flatMap(group => ys.map((key, index) => {
    const lines = choices.groupBy ? groupedRunSeries(group.runs,key,{...settings,smoothing:0}) : group.runs.map(run => ({points:seriesFor(run,key,settings)}));
    return { name: ys.length > 1 ? `${group.name} · ${key}` : group.name,
      color: group.runs[0].color, dashed:index > 0, key:`${group.key}:${key}`,
      points:choices.groupBy ? smoothSeries(aggregateSeries(lines,choices),settings) : lines[0].points };
  })).filter(series => series.points.length);
  }catch(problem){if(problem.code!=='SHARED_WRITER_LOCAL_AXIS')throw problem;all=[];error=problem.message;}
  const series = all.filter(series => !hidden.has(series.key));
  const axis=effectiveAxis(plottedRuns,ys,settings.xAxis);
  const options = { ...settings, title: settings.title || sourceMetric(metric), xLabel: settings.xLabel || axis.label, timeAxis:axis.timeAxis, mixedAxes:axis.mixedAxes, sharedWriters:plottedRuns.some(run=>run.writerLabel), zoom: state.zoom.get(metric) };
  options.groupBy=choices.groupBy;options.availableRuns=matchingRuns.length;options.shownRuns=choices.groupBy?groups.reduce((sum,g)=>sum+g.runs.length,0):Math.min(matchingRuns.length,maxRuns);
  return { all, series, options, spec: chartSpec(series, options), error };
}
function renderMetricCard(metric, runs, expanded = false) {
  if(panelCatalog().get(metric)?.type==='run-comparer')return renderRunComparer({id:metric,title:panelTitle(metric),runs,settings:state.panelSettings.get(panelKey(state.project,metric)),placement:panelOptions(layout(),metric),columns:state.columns,expanded,icon,valueCell});
  if(panelCatalog().get(metric)?.type!=='line')return renderLoggedPanel(metric,runs,expanded);
  const id = `${expanded ? 'expanded-' : ''}metric-${chartRegistry.size}`;
  const { all, series, options, spec, error } = metricPanel(metric, runs);
  chartRegistry.set(id, { spec, key: metric, metric, series });
  const total = series.reduce((sum, s) => sum + s.points.length, 0);
  const pinned = state.pinnedPanels.has(panelKey(state.project, metric)), custom = Object.keys(state.panelSettings.get(panelKey(state.project, metric)) || {}).length > 0;
  const label = options.title === metric && metric.includes('/') ? `<span class="prefix">${e(metric.split('/').slice(0, -1).join('/'))}/</span>${e(metric.split('/').at(-1))}` : e(options.title);
  const smoothing = ['line','area'].includes(options.kind) ? `<div class="chart-smoothing"><label><span>Smoothing</span><input type="range" data-panel-smoothing="${e(metric)}" aria-label="Smoothing for ${e(options.title)}" aria-valuetext="${options.smoothing ? options.smoothing.toFixed(2) : 'Off'}" min="0" max="0.98" step="0.01" value="${options.smoothing}" ${spec ? '' : 'disabled'}><output>${options.smoothing.toFixed(2)}</output></label></div>` : '';
  const legend = options.showLegend ? `<div class="panel-legend">${all.map(s => `<button class="legend-row ${series.includes(s) ? '' : 'muted-series'}" data-legend-metric="${e(metric)}" data-series="${e(s.key)}" aria-pressed="${series.includes(s)}" title="Toggle ${e(s.name)}"><span class="dot" style="background:${s.color}"></span><span class="legend-name">${e(s.name)}</span><span class="legend-value">${f(s.points.at(-1)?.y)}</span></button>`).join('')}</div>` : '';
  return `<article class="chart-card arranged-panel" data-metric="${e(metric)}" style="--panel-span:${Math.min(state.columns,panelOptions(layout(),metric).span || 1)};--panel-height:${panelOptions(layout(),metric).height || 270}px"><div class="chart-header">${CUSTOM_WORKSPACES?`<button class="panel-drag" draggable="true" data-panel-drag="${e(metric)}" aria-label="Drag ${e(metric)} panel" title="Drag panel">⠿</button>`:''}<h3 class="chart-title" title="${e(options.title)}">${label}</h3><div class="chart-actions">${CUSTOM_WORKSPACES?`<button class="icon-btn ${custom ? 'active' : ''}" data-edit-panel="${e(metric)}" title="Edit panel" aria-label="Edit ${e(metric)} panel">${icon('settings')}</button>`:''}${state.zoom.has(metric) ? `<button class="icon-btn" data-reset-chart="${e(metric)}" title="Reset zoom" aria-label="Reset zoom">${icon('reset')}</button>` : ''}${expanded ? '' : `<button class="icon-btn" data-expand="${e(metric)}" title="Expand chart" aria-label="Expand ${e(metric)}">${icon('expand')}</button>`}<button class="icon-btn" data-panel-menu="${e(metric)}" aria-label="Actions for ${e(panelTitle(metric))} panel" aria-haspopup="menu">⋯</button></div></div><div class="chart-subtitle">${series.length} ${options.groupBy ? 'group' : options.sharedWriters ? 'writer stream' : 'run'}${series.length !== 1 ? 's' : ''}${options.secondMetric ? ' / metric pairs' : ''} · ${f(total)} points${options.availableRuns>options.shownRuns?` · first ${options.shownRuns} of ${options.availableRuns} matching runs`:''} ${state.zoom.has(metric) ? '<span class="zoom-note">· zoomed</span>' : ''}${options.logScale ? ' · positive values only' : ''}${options.mixedAxes?' · mixed declared axes':''}${custom ? ' · custom settings' : ''}</div>${legend}${smoothing}<div class="chart-plot" data-chart="${id}">${error?`<div class="chart-empty" role="status">${e(error)}</div>`:renderChart(spec, id)}</div><div class="chart-footer"><span>${e(options.xLabel)}</span><span>Hover or use arrow keys to inspect</span>${expanded || !CUSTOM_WORKSPACES ? "" : `<button class="panel-resizer" data-panel-resize="${e(metric)}" aria-label="Resize ${e(metric)} panel" title="Drag to resize; arrow keys resize">◢</button>`}</div></article>`;
}
function axisName(axis = state.xAxis) { return ({ _step: 'Step', _runtime: 'Relative time (s)', _timestamp: 'Wall time', defined: 'Defined step' })[axis] || axis; }
/**
 * Purpose: Attach chart controls and fit each expanded plot to its viewport.
 * Input: The root that contains the chart elements.
 * Output: Each chart uses its displayed coordinates for inspection and zoom.
 */
function bindCharts(root) {
    for (const element of root.querySelectorAll('[data-chart]')) {
        const entry = expandedChartEntries.get(element) ||
            chartRegistry.get(element.dataset.chart);
        if (!entry) continue;
        if (chartDialog.contains(element)) {
            expandedChartEntries.set(element, entry);
            if (entry.spec) _fitExpandedChart(element, entry);
        }
        bindChart(element, entry.spec, range => {
            range ? state.zoom.set(entry.key, range) : state.zoom.delete(entry.key);
            if (chartDialog.open && entry.metric) expandChart(entry.metric);
            else if (root === detailDialog && detailDialog.open) {
                openRun(detailDialog.dataset.run, state.dialogTab);
            } else render();
        });
    }
}

/**
 * Purpose: Fit an expanded plot and keep keyboard focus on its replacement.
 * Input: The plot element and its chart registry entry.
 * Output: The entry and SVG use the same viewport for labels and interaction.
 */
function _fitExpandedChart(element, entry) {
    const svg = element.querySelector('.chart-svg');
    const hasFocus = svg === document.activeElement;
    const viewportWidth = svg.clientWidth, viewportHeight = svg.clientHeight;
    entry.spec = {...entry.spec, viewportWidth, viewportHeight};
    element.innerHTML = renderChart(entry.spec, element.dataset.chart);
    if (hasFocus) element.querySelector('.chart-svg').focus({preventScroll:true});
}

/** Redraw only this metric while the slider keeps focus and pointer capture.
 * Main callers: delegated input/change events. Compact and expanded copies stay
 * in sync, zoom is retained, and the existing browser preferences save on change.
 */
function updatePanelSmoothing(target) {
  const metric = target.dataset.panelSmoothing, smoothing = Number(target.value);
  if (!finite(smoothing) || smoothing < 0 || smoothing >= 1) return;
  const key = panelKey(state.project,metric), settings = state.panelSettings.get(key) || {};
  if (settings.smoothing === smoothing) return;
  state.panelSettings.set(key,{...settings,smoothing});
  const {all,series,spec} = metricPanel(metric,chartRuns());
  for (const card of document.querySelectorAll('.chart-card[data-metric]')) {
    if (card.dataset.metric !== metric) continue;
    const plot = card.querySelector('[data-chart]');
    const entry = expandedChartEntries.get(plot) || chartRegistry.get(plot?.dataset.chart);
    if (!entry) continue;
    Object.assign(entry, {spec, series});
    plot.innerHTML = renderChart(spec,plot.dataset.chart);
    bindCharts(card);
    const control = card.querySelector('[data-panel-smoothing]');
    control.value = smoothing;
    control.setAttribute('aria-valuetext',smoothing ? smoothing.toFixed(2) : 'Off');
    control.closest('label').querySelector('output').textContent = smoothing.toFixed(2);
    for (const row of card.querySelectorAll('[data-series]')) {
      const line = all.find(series=>series.key===row.dataset.series);
      if (line) row.querySelector('.legend-value').textContent = f(line.points.at(-1)?.y);
    }
  }
}

/** Use the same filtered/sorted runs as the workspace sidebar.
 * Main callers: renderMain. Config and summary columns keep qualified identity.
 */
function runColumns() {
  const fields = analysisFields(projectRuns()).filter(([key]) => key!=='run:name');
  const selected = state.runColumns?.map(key => /^(run|config|summary):/.test(key) ? key : `summary:${key}`)
    || ['run:state','run:created_at','run:group',...fields.filter(([key]) => key.startsWith('summary:') && /loss|accuracy|reward|score/.test(key)).slice(0,4).map(([key]) => key),'run:tags'];
  return fields.filter(([key]) => selected.includes(key));
}
function runColumnValue(run,field){
  if(field==='run:state')return stateBadge(run);
  if(field==='run:tags')return (run.tags || []).map(tag=>`<span class="tag">${e(tag)}</span>`).join(' ');
  if(['run:created_at','run:updated_at','run:finished_at'].includes(field))return `<span title="${e(fullDate(runField(run,field)))}">${e(compactDate(runField(run,field)))}</span>`;
  return valueCell(runField(run,field));
}
function renderRuns() {
  const columns = runColumns(), runs = filteredRuns(), choices = analysis();
  const perPage = layout().runPageSize || 20, pageCount = Math.ceil(runs.length / perPage); state.runPage = Math.max(0, Math.min(state.runPage, pageCount - 1));
  const head = (key, label) => `<th><button data-sort="${e(key)}">${e(label)} ${choices.sort === key ? choices.direction === 1 ? '↑' : '↓' : ''}</button></th>`;
  let currentGroup = null;
  const labels = new Map(groupRuns(runs, choices.groupBy).flatMap(group => group.runs.map(run => [run.key,group])));
  const rows = runs.slice(state.runPage * perPage, (state.runPage + 1) * perPage).map(run => {
    const group = labels.get(run.key);
    const header = choices.groupBy && group.key !== currentGroup ? `<tr class="run-group-row"><th colspan="${4+columns.length}">${e(group.name)} <span class="dim">${group.runs.length} runs</span></th></tr>` : '';
    currentGroup = group.key;
    return `${header}<tr data-view-key="${e(run.key)}"><td><input type="checkbox" data-run-select="${e(run.key)}" aria-label="Select ${e(run.name)}" ${state.selected.has(run.key) ? 'checked' : ''}></td><td>${visibilityControl(run)}</td><td>${colorControl(run)}</td><td><button class="table-run" data-run="${e(run.key)}">${e(run.name)}</button><button class="quick-inspect" data-inspect-run="${e(run.key)}" aria-label="Quick inspect ${e(run.name)}" title="Quick inspect">${icon("info")}<span class="sr-only">${e(run.name)}</span></button><span class="dim mono run-id">${e(run.id)}</span></td>${columns.map(([field]) => `<td class="mono">${runColumnValue(run,field)}</td>`).join('')}</tr>`;
  }).join('');
  return `<section class="runs-browser"><div class="runs-browser-title"><h2>Runs <span class="count">${projectRuns().length}</span></h2><button class="icon-btn" data-action="collapse-run-table" aria-label="Collapse runs to sidebar">${icon("left")}</button></div><div class="toolbar">${runSearch()}<label>State <select data-state="status"><option value="">All states</option>${['running','finished','failed','crashed'].map(status => `<option ${state.status === status ? 'selected' : ''}>${status}</option>`).join('')}</select></label>${analysisButtons()}<label>Group <select data-state="group" aria-label="Filter run group"><option value="">All groups</option>${[...new Set(projectRuns().map(run=>run.group).filter(Boolean))].sort().map(group=>`<option value="${e(group)}" ${state.group===group?'selected':''}>${e(group)}</option>`).join('')}</select></label><span class="result-count">${runs.length} runs · ${selectedRuns().length} selected</span><span class="grow"></span><button class="btn small" data-action="export-runs">${icon('download')} Export CSV</button>${selectedRuns().length?`<div class="bulk-run-actions" role="group" aria-label="Selected run actions"><span>${selectedRuns().length} selected</span><button class="btn small" data-action="show-selected-runs">Show in charts</button><button class="btn small" data-action="hide-selected-runs">Hide from charts</button><button class="btn small" data-view="compare">Compare selected</button><button class="icon-btn" data-action="clear-row-selection" aria-label="Clear run selection">${icon("close")}</button></div>`:""}</div><div class="table-wrap runs-table"><table><thead><tr><th><input type="checkbox" data-action="select-all" aria-label="Select all matching runs" ${runs.length && runs.every(run => state.selected.has(run.key)) ? 'checked' : ''}></th><th>${visibilityMenu()}</th><th aria-label="Run color"></th>${head('run:name','Run')}${columns.map(([field,label]) => head(field,label)).join('')}</tr></thead><tbody>${rows || `<tr><td colspan="${4+columns.length}" class="chart-empty">No runs match these filters.</td></tr>`}</tbody></table></div>${runPagination(runs.length)}<p class="table-note">Eyes control chart visibility. Checkboxes select runs for actions.</p></section>`;
}
function manageRunColumns() {
  const fields = analysisFields(projectRuns()).filter(([key]) => key!=='run:name'), selected = new Set(runColumns().map(([key]) => key));
  const pins=new Set(layout().pinnedColumns || []);
  inspectDialog.className = 'small-dialog';
  inspectDialog.innerHTML = `<header class="dialog-header"><h2>Run columns</h2><button class="icon-btn" data-action="close-dialog" aria-label="Close run column settings">${icon('close')}</button></header><div class="dialog-body"><p class="dim">Visible columns appear in the full Runs table. Pin a visible column to also show its values beside the charts.</p><input type="search" data-column-search placeholder="Search columns" aria-label="Search columns"><div class="column-scope-head"><span>Column</span><span>Visible</span><span>Pinned</span></div><div class="column-list">${fields.map(([key,label]) => `<div class="column-choice" data-column-label="${e(label.toLowerCase())}"><span>${e(label)}</span><input type="checkbox" data-run-column="${e(key)}" aria-label="Show ${e(label)} in full table" ${selected.has(key) ? 'checked' : ''}><input type="checkbox" data-pin-column="${e(key)}" aria-label="Pin ${e(label)} in sidebar" ${pins.has(key)?'checked':''} ${selected.has(key)?'':'disabled'}></div>`).join('') || '<p>No configuration or summaries recorded.</p>'}</div></div><footer class="dialog-footer"><span class="dim">Name always remains visible.</span><button class="btn primary" data-action="close-dialog">Done</button></footer>`;
  inspectDialog.showModal();
}

/** Edit grouping or sorting without changing the selected runs themselves.
 * Main callers: Group and Sort actions on both run selectors.
 */
function openAnalysisSettings(kind) {
  const choices = analysis(), fields = analysisFields(projectRuns());
  const options = (items,value) => items.map(([key,label]) => `<option value="${e(key)}" ${key === value ? 'selected' : ''}>${e(label)}</option>`).join('');
  const select = (label,key,items) => `<label>${label}<select data-analysis="${key}" aria-label="${label}">${options(items,choices[key])}</select></label>`;
  inspectDialog.className = 'small-dialog';
  const controls = kind === 'grouping'
    ? select('Group by','groupBy',[['','No grouping'],['all','All selected runs'],...fields]) + select('Group aggregation','aggregation',[['mean','Mean'],['median','Median'],['min','Minimum'],['max','Maximum']]) + select('Variation band','range',[['stddev','Standard deviation'],['stderr','Standard error'],['minmax','Minimum / maximum'],['none','None']])
    : select('Sort by','sort',fields) + select('Sort direction','direction',[[1,'Ascending'],[-1,'Descending']]);
  inspectDialog.innerHTML = `<header class="dialog-header"><h2>${kind === 'grouping' ? 'Group runs' : 'Sort runs'}</h2><button class="icon-btn" data-action="close-dialog" aria-label="Close analysis settings">${icon('close')}</button></header><div class="dialog-body analysis-settings">${controls}${kind === 'grouping' ? '<p class="dim">Group charts use recorded X coordinates. Missing samples are not interpolated. Hover shows the contributing run count.</p>' : ''}</div><footer class="dialog-footer"><button class="btn primary" data-action="close-dialog">Done</button></footer>`;
  inspectDialog.showModal();
}

function renderCompare() {
  const runs = selectedRuns();
  if (runs.length < 2) return `<div class="workspace">${renderRunPanel()}<div class="empty">${icon('compare')}<h2>Compare experiments side by side.</h2><p>Select at least two runs to compare their configuration and final metrics.</p><button class="btn" data-action="select-all">Select matching runs</button></div></div>`;
  const config = runs.map(run => new Map(flatten(run.config))), summary = runs.map(run => new Map(flatten(run.summary).filter(([, value]) => (!isRich(value) || value._type === 'number'))));
  const section = (title, values) => {
    const pins = title === 'Configuration' ? runs.flatMap(run => run.pinned_config_keys) : [];
    const rows = comparisonRows(values, { pins, differences: state.differences, search: state.compareSearch });
    return `<tr><th colspan="${runs.length + 1}" class="eyebrow comparison-section"><span>${title}</span></th></tr>${rows.map(({ key, values: cells, present, different }) => `<tr><th scope="row" class="mono" ${isPinnedConfigKey(key, pins) ? 'data-pinned-config="true" title="Pinned configuration"' : ''}><span class="comparison-key">${isPinnedConfigKey(key, pins) ? icon('pin') + ' ' : ''}${e(key)}</span></th>${cells.map((value, index) => `<td class="mono ${different ? 'diff-cell' : ''}">${!present[index] ? '<span class="dim">Not logged</span>' : value === null ? '<span class="dim mono">null</span>' : valueCell(value)}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${runs.length + 1}" class="dim">${state.compareSearch ? 'No matching fields.' : state.differences ? 'No differences recorded.' : 'No values recorded.'}</td></tr>`}`;
  };
  return `<div class="workspace">${renderRunPanel()}<div class="charts-area"><div class="config-controls">${searchBox('compareSearch', state.compareSearch, 'Find comparison fields')}<label><input type="checkbox" data-state="differences" ${state.differences ? 'checked' : ''}> Show differences only</label><span class="result-count">${runs.length} runs</span></div><div class="table-wrap comparison-table" tabindex="0" role="region" aria-label="Run comparison, scroll horizontally for more runs" data-comparison-scroll="true"><table><thead><tr><th scope="col">Parameter / metric</th>${runs.map(run => `<th><button class="table-run" data-run="${e(run.key)}"><span class="dot" style="background:${run.color}"></span>${e(run.name)}</button></th>`).join('')}</tr></thead><tbody>${section('Configuration', config)}${section('Summary', summary)}</tbody></table></div><p class="table-note">Highlighted cells differ between runs. Not logged means a field was absent; null is an explicitly recorded value. Focus the table and use arrow keys to scroll.</p><br></div></div>`;
}

function summaryMetrics(runs) {
  const rows = new Map(runs.map(run => [run.key, new Map(flatten(run.summary).filter(([, value]) => finite(value) || value?._type === 'number'))]));
  return { rows, keys: [...new Set([...rows.values()].flatMap(row => [...row.keys()]))].sort() };
}

function valueCell(value) {
  if (value === undefined || value === null) return '<span class="dim">·</span>';
  if (finite(value)) return e(f(value));
  if (value?._type === 'number') return `<span class="tag nonfinite" title="Nonfinite numeric value">${e(value.value)}</span>`;
  if (isRich(value)) return `<span class="tag">${e(value._type)}</span>`;
  if (typeof value === 'object') return `<pre>${e(JSON.stringify(value, null, 2))}</pre>`;
  return e(value);
}

function renderLoggedPanel(id,runs,expanded=false) {
  return renderTypedPanel({id,definition:panelCatalog().get(id),runs,expanded,
    settings:state.panelSettings.get(panelKey(state.project,id)),placement:panelOptions(layout(),id),columns:state.columns,
    icon,renderValue:renderRichValue,steps:state.richSteps,register:(key,value)=>richRegistry.set(key,value)});
}
function openPanelLibrary(section) {
  showPanelLibrary({dialog:inspectDialog,sources:projectSourceCache.get(state.project) || discoverPanelSources(projectRuns()),section,
    onAdd:panel=>{layout().panels ||= [];layout().panels.push(panel);setPanelOptions(layout(),panel.id,{section:panel.section || defaultSection(panel.source)});state.collapsedSections.delete(panelKey(state.project,panel.section || defaultSection(panel.source)));renderedCatalog=null;persist();render();}});
}
function openLoggedPanelEditor(id) {
  showLoggedPanelEditor({dialog:panelDialog,definition:panelCatalog().get(id),settings:state.panelSettings.get(panelKey(state.project,id)),
    sources:projectSourceCache.get(state.project) || discoverPanelSources(projectRuns()),onApply:value=>{state.panelSettings.set(panelKey(state.project,id),value);persist();render();}});
}

function renderData() {
  const runs = visibleRuns();
  const dataRuns = state.richRun ? runs.filter(run => run.key === state.richRun) : runs;
  const entries = dataRuns.flatMap(run => loggedValues(run).map(rich => ({ run, ...rich })));
  const types = [...new Set(entries.map(entry => entry.value?._type || (Array.isArray(entry.value) ? 'list' : 'text')))].sort();
  const shown = entries.filter(entry => (!state.richType || (entry.value?._type || (Array.isArray(entry.value) ? 'list' : 'text')) === state.richType) && (!state.richSearch || entry.key.toLowerCase().includes(state.richSearch.toLowerCase())));
  const retainedRun = state.richRun && !runs.some(run => run.key === state.richRun);
  const retainedType = state.richType && !types.includes(state.richType);
  const activeFilters = Boolean(state.richRun || state.richType || state.richSearch);
  const filters = `<div class="data-filters"><select data-state="richRun" aria-label="Logged data run"><option value="">All selected runs</option>${retainedRun ? `<option value="${e(state.richRun)}" selected>${e(model.runs.find(run => run.key === state.richRun)?.name || state.richRun)} (outside selection)</option>` : ''}${runs.map(run => `<option value="${e(run.key)}" ${state.richRun === run.key ? 'selected' : ''}>${e(run.name)}</option>`).join('')}</select><select data-state="richType" aria-label="Logged data type"><option value="">All types</option>${retainedType ? `<option value="${e(state.richType)}" selected>${e(state.richType)} (no matching values)</option>` : ''}${types.map(type => `<option value="${e(type)}" ${state.richType === type ? 'selected' : ''}>${e(type)}</option>`).join('')}</select>${searchBox('richSearch', state.richSearch, 'Find logged values')}<span class="result-count">${shown.length} logged values</span>${activeFilters ? '<button class="btn small" data-action="clear-data-filters">Clear data filters</button>' : ''}</div>`;
  const cards = shown.map(entry => {
    const id = `${entry.run.key}:${entry.writer || 'single'}:${entry.key}`, history = entry.run.history.filter(row => (!entry.writer || row._writer === entry.writer) && valueAtKey(row, entry.key) !== undefined);
    const chosenStep = state.richSteps.get(id);
    const selected = chosenStep !== undefined ? history.find(row => String(row._step) === String(chosenStep)) : null;
    const value = selected ? tableAtStep(entry.run, entry.key, selected._step, entry.writer) : entry.value;
    const step = selected ? selected._step : entry.step;
    richRegistry.set(id, value);
    const position = selected ? history.indexOf(selected) : history.length - 1;
    return `<article data-view-key="${e(id)}" class="rich-card ${value?._type === 'table' || value?._type === 'html' ? 'wide' : ''}"><header class="rich-header"><span class="dot" style="background:${entry.run.color}"></span><h3 class="chart-title" title="${e(entry.key)}">${e(entry.key)}</h3><span class="tag">${e(value?._type || 'text')}</span></header><div class="rich-body"><div class="rich-history"><button class="text-button" data-run="${e(entry.run.key)}">${e(entry.run.name)}</button>${entry.writer ? `<span class="tag">${e(entry.writer_label || entry.writer)}</span>` : ''}<span>·</span>${entry.source === 'summary' ? '<span class="tag">Summary · latest value</span>' : `<button class="icon-btn" data-rich-id="${e(id)}" data-history-step="${e(history[position - 1]?._step ?? '')}" ${position <= 0 ? 'disabled' : ''} aria-label="Previous snapshot of ${e(entry.key)}">${icon('left')}</button><label>Step <select data-rich-step="${e(id)}" aria-label="History step for ${e(entry.key)}"><option value="">Latest (${e(entry.step)})</option>${history.slice().reverse().map(row => `<option value="${e(row._step)}" ${selected && row._step === step ? 'selected' : ''}>${e(row._step)}</option>`).join('')}</select></label><button class="icon-btn" data-rich-id="${e(id)}" data-history-step="${e(history[position + 1]?._step ?? '')}" ${position >= history.length - 1 ? 'disabled' : ''} aria-label="Next snapshot of ${e(entry.key)}">${icon('chevron')}</button>`}<button class="icon-btn" style="margin-left:auto" data-export-rich="${e(id)}" title="Download logged value" aria-label="Download ${e(entry.key)}">${icon('download')}</button></div>${value?.title ? `<h3 class="custom-plot-title">${e(value.title)}</h3>` : ''}${renderRichValue(value, id, entry.run.color)}${value?.caption && value._type !== 'image' ? `<p class="rich-caption">${e(value.caption)}</p>` : ''}${value?.split_table && value.table ? `<details class="chart-table-disclosure"><summary>Underlying chart table</summary>${renderTable(value.table, id + ':source-table')}</details>` : ''}</div></article>`;
  }).join('');
  return `<div class="workspace">${renderRunPanel()}<div class="charts-area">${filters}${pathNote(entries.map(entry => entry.key))}${cards ? `<div class="rich-grid">${cards}</div><br>` : `<div class="empty">${icon('data')}<h2>${runs.length ? 'No logged data in this view.' : 'Choose runs to inspect.'}</h2><p>Tables, histograms, custom plots, images, audio, and video appear here when logged. Numeric metrics are in the workspace.</p><button class="btn" data-view="workspace">View metrics</button></div>`}</div></div>`;
}

/** Turn known SDK rich values into safe, self-contained views.
 * Main callers: renderData, table cells, run detail. Arbitrary objects remain
 * readable JSON. HTML is isolated in a sandbox with no script permission.
 */
function renderRichValue(value, id, color = '#8abbff', context = null) {
  if (Array.isArray(value)) {
    if (value.some(isRich)) return `<div class="media-grid">${value.map((item, i) => `<div>${renderRichValue(item, `${id}:${i}`, color, context)}${item?.caption && item._type !== 'image' ? `<div class="rich-caption">${e(item.caption)}</div>` : ''}</div>`).join('')}</div>`;
    return `<pre class="json-view">${e(JSON.stringify(value, null, 2))}</pre>`;
  }
  if (!value || typeof value !== 'object') return `<pre class="json-view">${e(value)}</pre>`;
  if (value._backprop_external_reference) return `<div class="notice"><span>External artifact reference<code class="external-reference-uri">${e(value.uri || value.ref || '')}</code>Download through the Python SDK with the provider's credentials. These bytes are not stored in Foundry.</span></div>`;
  if (value._load_error) return `<div class="notice error">${e(value._load_error)}</div>`;
  if (value._type === 'python.sequence') return `<details class="array-cell" data-view-key="${e(id)}"><summary>${e(value.kind || 'Sequence')} · ${Array.isArray(value.data) ? value.data.length : 0} items</summary><pre class="json-view">${e(JSON.stringify(value.data, null, 2))}</pre></details>`;
  if (value._type === 'timestamp' && typeof value.value === 'string') return `<time class="table-timestamp mono" datetime="${e(value.value)}">${e(value.value.replace('T',' ').replace(/Z$/,' UTC'))}</time>`;
  if (value._type === 'ndarray') {
    const dtype = typeof value.dtype === 'string' ? value.dtype : 'array';
    const shape = Array.isArray(value.shape) && value.shape.every(size => Number.isSafeInteger(size) && size >= 0) ? value.shape.length ? value.shape.join(' × ') : 'scalar' : 'unknown shape';
    return `<details class="array-cell" data-view-key="${e(id)}"><summary>${e(dtype)} · ${e(shape)}</summary><pre class="json-view">${e(JSON.stringify(value.data, null, 2))}</pre></details>`;
  }
  if (value._type === 'table') {
    if (value.external && !value.data) return `<div class="notice">${icon('info')}<span>This table has not finished loading. Refresh to retry.</span></div>`;
    if (context) tableContexts.set(id, context);
    const evaluation = value.evaluation_roles;
    const header = evaluation ? `<div class="notice"><span>Evaluation table · Match by ${e(evaluation.input_columns.length ? evaluation.input_columns.join(', ') : 'row number (1-based)')}<br>${Object.entries(value.evaluation_scores || {}).map(([key,score]) => `${e(key)}: mean ${f(score.mean)} (${score.count} scores)`).join(' · ')}</span></div>` : '';
    return header + renderTable(value, id);
  }
  if (value._type === 'histogram') {
    const points = (value.values || []).map((count, i) => ({ x: ((value.bins || [])[i] + (value.bins || [])[i + 1]) / 2, y: count, width: (value.bins || [])[i + 1] - (value.bins || [])[i] }));
    return renderRichChart([{ name: 'Count', color, points }], { title: 'Distribution', kind: 'bar', xLabel: 'Value' }, id);
  }
  if (value._type === 'number') return valueCell(value);
  if (value._type === 'vega') {
    vegaRegistry.set(id, value);
    return `<div class="plotly-container" data-vega-id="${e(id)}" data-vega-key="${e(JSON.stringify([id,value]))}"><div class="chart-empty content-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>Loading custom chart…</span></div></div>`;
  }
  if (value._type === 'plot') return renderCustomPlot(value, id, color);
  if (value._type === 'plotly') return renderPlotly(value, id, color);
  if (['image', 'audio', 'video', 'html', 'graph', 'graph-file'].includes(value._type)) {
    const mediaId = `media-${++mediaSequence}`;
    mediaRegistry.set(mediaId, value);
    return `<div class="media-container" data-media="${mediaId}" data-media-key="${e(JSON.stringify([id, value]))}"><div class="chart-empty content-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>Loading ${e(value._type)}…</span></div></div>`;
  }
  return `<pre class="json-view">${e(JSON.stringify(value, null, 2))}</pre>`;
}

function renderTable(value, id) {
  tableRegistry.set(id, value);
  if (!tableContexts.has(id)) tableContexts.set(id, { tables: snapshotTables(value), title: value.table_id || 'Logged table' });
  const settings = state.tableOptions.get(id) || {}, filter = tableFilters.get(id) || '', rows = tableRows(value, { ...settings, search: filter });
  const limit = settings.limit || 50, page = Math.max(0, Math.min(state.tablePages.get(id) || 0, Math.ceil(rows.length / limit) - 1));
  const columns = (value.columns || []).map((name, index) => ({ name, index })).filter(({ index }) => !settings.hiddenColumns?.includes(index));
  state.tablePages.set(id, page);
  const legacy = value.legacy_offline ? `<div class="notice table-legacy"><span>Legacy offline view · ${value.data?.length || 0} visible rows · ${e(value.nrows ?? value.data?.length ?? 0)} table rows logged.${value.log_mode === 'INCREMENTAL' ? ' This view contains the latest increment.' : ''}</span>${value.artifact_snapshot ? `<button class="btn small" data-artifact-snapshot="${e(id)}">Full artifact snapshot</button>` : ''}</div>` : '';
  return `${legacy}<div class="toolbar table-toolbar"><div class="search">${icon('search')}<input type="search" data-table-search="${e(id)}" placeholder="Search table rows" aria-label="Search table rows" value="${e(filter)}"></div><select data-table-option="${e(id)}" data-option="column" aria-label="Search in column"><option value="">All columns</option>${(value.columns || []).map((name, index) => `<option value="${index}" ${String(settings.column) === String(index) ? 'selected' : ''}>${e(name)}</option>`).join('')}</select><span class="result-count">${rows.length} / ${value.data?.length || 0} rows</span><span class="grow"></span><button class="btn small" data-table-columns="${e(id)}">Columns</button><button class="icon-btn" data-export-table="${e(id)}" title="Export filtered table" aria-label="Export filtered table">${icon('download')}</button></div><div class="table-wrap logged-table" data-view-key="${e(id)}"><table><thead><tr><th>Row</th>${columns.map(({ name, index }) => `<th aria-sort="${settings.sort === index ? settings.direction < 0 ? 'descending' : 'ascending' : 'none'}"><button data-table-sort="${e(id)}" data-column="${index}">${e(name)} <span class="dim">${settings.sort === index ? settings.direction < 0 ? '↓' : '↑' : '↕'}</span></button>${renderColumnType(value.column_types?.[index], id, index, name)}</th>`).join('')}</tr></thead><tbody>${rows.slice(page * limit, (page + 1) * limit).map(({ cells, index }) => `<tr data-view-key="${index}"><td class="dim mono"><button class="row-inspect" data-inspect-table="${e(id)}" data-row="${index}" aria-label="Inspect row ${index}">${index} ${icon('expand')}</button></td>${columns.map(({ index: column }) => `<td>${renderTableCell(value, id, index, column)}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${columns.length + 1}" class="dim">${value.data?.length ? 'No matching rows. Clear the search or choose another column.' : 'This saved table has no rows.'}</td></tr>`}</tbody></table></div><div class="table-pagination"><span>${page * limit + (rows.length ? 1 : 0)}-${Math.min((page + 1) * limit, rows.length)} of ${rows.length}${value.log_mode ? ` · ${e(value.log_mode)}` : ''}</span><label>Rows <select data-table-option="${e(id)}" data-option="limit" aria-label="Rows per page">${[25,50,100].map(count => `<option ${limit === count ? 'selected' : ''}>${count}</option>`).join('')}</select></label><span><button class="btn small" data-table-page="${e(id)}" data-delta="-1" ${page === 0 ? 'disabled' : ''}>Previous</button> <button class="btn small" data-table-page="${e(id)}" data-delta="1" ${(page + 1) * limit >= rows.length ? 'disabled' : ''}>Next</button></span></div><p class="table-note">Click a row number to inspect full text and media. Click a column heading to sort.</p>`;
}

/** Show a compact type label and expose structured schemas as inert JSON.
 * Main callers: table headers; the delegated click handler opens the inspector.
 */
function renderColumnType(schema, id, index, name) {
  const { label, details } = columnType(schema);
  if (!label) return '';
  return details ? `<button class="column-type schema-type" data-inspect-schema="${e(id)}" data-column="${index}" aria-label="Inspect type for ${e(name)}">${e(label)} ${icon('info')}</button>` : `<span class="column-type">${e(label)}</span>`;
}

function openColumnType(id, index) {
  const table = tableRegistry.get(id), schema = columnType(table?.column_types?.[index]);
  if (!schema.details) return;
  inspectDialog.className = 'small-dialog';
  inspectDialog.innerHTML = `<header class="dialog-header"><div><div class="eyebrow">Column type</div><h2>${e(table.columns[index])}</h2></div><button class="icon-btn" data-action="close-dialog" aria-label="Close type inspector">${icon('close')}</button></header><div class="dialog-body"><p class="table-note">${e(schema.label)} · Complete logged schema</p><pre class="schema-json">${e(schema.details)}</pre></div><footer class="dialog-footer"><button class="btn primary" data-action="close-dialog">Done</button></footer>`;
  if (!inspectDialog.open) inspectDialog.showModal();
}

/** Render a cell link only when the SDK declares a relationship.
 * Main callers: table grids and row inspectors. Missing targets stay visible
 * with an explanation; links never guess among tables in different runs.
 */
function renderTableCell(table, id, row, column, expanded = false) {
  const value = table.data[row][column], context = tableContexts.get(id) || {};
  const relation = tableRelation(table, column, row, context);
  if (relation) return `<div class="linked-cell-wrap"><button class="linked-cell" data-linked-table="${e(id)}" data-row="${row}" data-column="${column}" ${relation.error ? 'disabled' : ''} title="${e(relation.error || `Inspect linked row in ${relation.title}`)}" aria-label="${e(`Inspect ${table.columns[column]} link: ${String(value ?? 'empty')}`)}">${valueCell(value)} ${icon('arrow')}</button>${relation.error ? `<span class="link-unavailable">${e(relation.error)}</span>` : ''}</div>`;
  const content = expanded || isRich(value) ? renderRichValue(value, `${id}:${row}:${column}`, '#8abbff', context) : `<div class="table-cell">${valueCell(value)}</div>`;
  const nested = nestedTableRelations(table,column,row,context);
  return `${content}${nested.length ? `<details class="nested-relations" data-view-key="${e(JSON.stringify([id,row,column]))}" ${expanded ? 'open' : ''}><summary>${nested.length} nested table ${nested.length===1?'link':'links'}</summary>${nested.map((link,index)=>`<div><button class="linked-cell" data-linked-table="${e(id)}" data-row="${row}" data-column="${column}" data-relation-index="${index}" ${link.error?'disabled':''} title="${e(link.error || link.title)}">${e(link.label)}${link.choice?` · ${e(link.choice)}`:''}: ${e(String(link.value ?? 'empty'))} ${icon('arrow')} <span class="dim">${e(link.title)}</span></button>${link.error?`<p class="link-unavailable">${e(link.error)}</p>`:''}</div>`).join('')}</details>` : ''}`;
}

function renderRowFields(table, id, index) {
  return `<div class="row-fields">${table.columns.map((column, col) => `<section><header><h3>${e(column)}</h3><span class="dim">${e(columnType(table.column_types?.[col]).label || (table.data[index][col] === null ? 'null' : typeof table.data[index][col]))}</span></header>${renderTableCell(table, id, index, col, true)}</section>`).join('')}</div>`;
}

function openTableRow(id, index) {
  const table = tableRegistry.get(id); if (!table?.data?.[index]) return;
  if (tableExplorer.openRow(id, index)) return;
  if (tableInExplorer(id)) { const context = tableContexts.get(id); tableExplorer.openChild({ id, table, context, title: table.table_id || 'Nested table', row: index }); return; }
  const rows = tableRows(table, { ...(state.tableOptions.get(id) || {}), search: tableFilters.get(id) || '' });
  const position = rows.findIndex(row => row.index === index);
  inspectDialog.className = 'row-inspector';
  inspectDialog.innerHTML = `<header class="dialog-header"><div><div class="eyebrow">Table row</div><h2>Row ${index}</h2></div><button class="icon-btn" data-action="close-dialog" aria-label="Close row inspector">${icon('close')}</button></header><div class="dialog-body">${renderRowFields(table, id, index)}</div><footer class="dialog-footer"><span class="dim">${position + 1} of ${rows.length} matching rows</span><span class="grow"></span><button class="btn" data-inspect-table="${e(id)}" data-row="${rows[position - 1]?.index ?? 0}" ${position <= 0 ? 'disabled' : ''}>Previous row</button><button class="btn" data-inspect-table="${e(id)}" data-row="${rows[position + 1]?.index ?? 0}" ${position >= rows.length - 1 ? 'disabled' : ''}>Next row</button></footer>`;
  if (!inspectDialog.open) inspectDialog.showModal(); hydrateMedia(inspectDialog);
}

function openTableColumns(id) {
  const table = tableRegistry.get(id), settings = state.tableOptions.get(id) || {};
  inspectDialog.className = 'small-dialog';
  inspectDialog.innerHTML = `<header class="dialog-header"><h2>Visible columns</h2><button class="icon-btn" data-action="close-dialog" aria-label="Close column settings">${icon('close')}</button></header><div class="dialog-body column-list">${table.columns.map((column, index) => `<label><input type="checkbox" data-column-table="${e(id)}" value="${index}" ${settings.hiddenColumns?.includes(index) ? '' : 'checked'}> ${e(column)}</label>`).join('')}</div><footer class="dialog-footer"><span class="dim">Changes apply immediately to this table.</span><button class="btn primary" data-action="close-dialog">Done</button></footer>`;
  inspectDialog.showModal();
}

function openPanel(metric) {
  if(panelCatalog().get(metric)?.type!=='line'){openLoggedPanelEditor(metric);return;}
  openPanelEditor({ dialog: panelDialog, metric:sourceMetric(metric),groupFields:analysisFields(projectRuns()), metrics: metricKeys(chartRuns(), true), defaults: {...workspaceDefaults(),...sectionOptions(layout(),metricSection(metric)).settings},
    settings: {...(sourceMetric(metric)!==metric?{yMetric:sourceMetric(metric)}:{}),...state.panelSettings.get(panelKey(state.project, metric))},
    renderPreview: (element, settings) => { const { spec, series } = metricPanel(metric, chartRuns(), settings); element.innerHTML = `<h3>${e(settings.title || sourceMetric(metric))}</h3><div class="chart-plot">${renderChart(spec, 'preview')}</div><p class="dim">${series.length} series · ${settings.smoothing ? 'Exponential smoothing' : 'Unsmoothed values'}</p>`; bindChart(element.querySelector('.chart-plot'), spec, () => {}); },
    onApply: settings => { state.panelSettings.set(panelKey(state.project, metric), settings); state.zoom.delete(metric); persist(); render(); if (chartDialog.open) expandChart(metric); toast('Panel settings applied.'); } });
}

function managePanels() {
  const metrics = allPanels(projectRuns());
  inspectDialog.className = 'panel-manager';
  inspectDialog.innerHTML = `<header class="dialog-header"><div><div class="eyebrow">Personal workspace</div><h2>Panels</h2></div><button class="icon-btn" data-action="close-dialog" aria-label="Close panel manager">${icon('close')}</button></header><div class="dialog-body"><p class="table-note">Choose which metrics appear. Hidden panels keep their settings and logged data.</p><div class="panel-list">${metrics.map(metric => `<div><label><input type="checkbox" data-panel-visible="${e(metric)}" ${state.hiddenPanels.has(panelKey(state.project, metric)) ? '' : 'checked'}><span class="mono">${e(panelTitle(metric))}</span></label><button class="icon-btn ${state.pinnedPanels.has(panelKey(state.project, metric)) ? 'active' : ''}" data-pin="${e(metric)}" title="Pin ${e(metric)}" aria-label="Pin ${e(metric)}">${icon('pin')}</button></div>`).join('')}</div></div><footer class="dialog-footer"><button class="btn" data-action="restore-panels">Show all panels</button><span class="grow"></span><button class="btn primary" data-action="close-dialog">Done</button></footer>`;
  if (!inspectDialog.open) inspectDialog.showModal();
}

function renderRichChart(series, options, key) {
  const id = `rich-${chartRegistry.size}`;
  const spec = chartSpec(series.map(s => { const points = s.points.filter(p => finite(p.x) && finite(p.y)); return { ...s, points: options.preserveOrder ? points : points.sort((a, b) => a.x - b.x) }; }), { ...options, zoom: state.zoom.get(key) });
  chartRegistry.set(id, { spec, key, series });
  return `<div class="chart-plot" data-chart="${id}">${renderChart(spec, id)}</div><div class="chart-footer"><span>${e(options.xLabel || 'Value')}${options.yLabel ? ` / ${e(options.yLabel)}` : ''}${options.logX ? ' · Log X' : ''}${options.logScale ? ' · Log Y' : ''}</span><span>Drag to zoom · Double click to reset</span></div>${options.showLegend !== false && series.length > 1 ? `<div class="chart-footer"><div class="legends">${series.map(s => `<span class="legend"><span class="dot" style="background:${s.color}"></span><span>${e(s.name)}</span></span>`).join('')}</div></div>` : ''}`;
}

function renderCustomPlot(value, id, color) {
  const table = value.table || {}, columns = table.columns || [], rows = table.data || [], fields = value.fields || {};
  const index = field => columns.indexOf(fields[field]);
  if (value.kind === 'confusion_matrix') return renderConfusion(value);
  if (value.kind === 'histogram') {
    const fieldIndex = index('value') >= 0 ? index('value') : index('x');
    const data = rows.map(row => row[fieldIndex]).filter(finite);
    if (!data.length) return '<div class="chart-empty">No numeric histogram values.</div>';
    const min = data.reduce((a, b) => Math.min(a, b)), max = data.reduce((a, b) => Math.max(a, b));
    const width = (max - min || 1) / 24, counts = Array(24).fill(0);
    data.forEach(number => counts[Math.min(23, Math.floor((number - min) / width))]++);
    return renderRichChart([{ name: 'Count', color, points: counts.map((count, i) => ({ x: min + (i + .5) * width, y: count, width })) }], { title: value.title, kind: 'bar', xLabel: fields.value || fields.x || 'Value' }, id);
  }
  const xIndex = index('x'), yIndex = index('y'), seriesIndex = index('series');
  if (xIndex < 0 || yIndex < 0) return `<div class="notice">This plot's column mapping is unavailable.</div>${renderTable(table, id)}`;
  const categories = rows.some(row => typeof row[xIndex] === 'string') ? [...new Set(rows.map(row => String(row[xIndex])))] : null;
  const groups = new Map();
  for (const row of rows) {
    const name = seriesIndex >= 0 ? String(row[seriesIndex]) : value.title || fields.y || 'Series';
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push({ x: categories ? categories.indexOf(String(row[xIndex])) : row[xIndex], y: row[yIndex] });
  }
  const palette = [color, '#6ac9d0', '#b49ae8', '#fbb360', '#4c90f0', '#ee8f9b'];
  return renderRichChart([...groups].map(([name, points], i) => ({ name, points, color: palette[i % palette.length] })), { title: value.title, kind: ['line_series', 'roc_curve', 'pr_curve'].includes(value.kind) ? 'line' : value.kind, categories, xLabel: value.string_fields?.xname || fields.x, ...(value.kind === 'roc_curve' || value.kind === 'pr_curve' ? { xMin: 0, xMax: 1, yMin: 0, yMax: 1 } : {}) }, id);
}

function renderConfusion(value) {
  const { columns = [], data = [] } = value.table || {}, fields = value.fields || {};
  const xi = columns.indexOf(fields.x), yi = columns.indexOf(fields.y), vi = columns.indexOf(fields.value);
  const xs = [...new Set(data.map(row => row[xi]))], ys = [...new Set(data.map(row => row[yi]))];
  const max = data.reduce((acc, row) => Math.max(acc, row[vi] || 0), 1);
  return `<p class="muted" style="font-size:10px;margin-bottom:10px">Rows: actual · Columns: predicted</p><div class="table-wrap"><table><thead><tr><th>Actual / predicted</th>${xs.map(x => `<th>${e(x)}</th>`).join('')}</tr></thead><tbody>${ys.map(y => `<tr><th>${e(y)}</th>${xs.map(x => { const row = data.find(row => row[xi] === x && row[yi] === y), count = row?.[vi] || 0; return `<td class="mono" style="background:rgba(185,231,120,${(count / max * .45).toFixed(3)})">${f(count)}</td>`; }).join('')}</tr>`).join('')}</tbody></table></div>`;
}

function renderPlotly(value, id, color) {
  plotlyRegistry.set(id, {id, spec:value.spec, resources:value.resources});
  return `<div class="plotly-container" data-plotly-id="${e(id)}" data-plotly-key="${e(id)}"><div class="chart-empty content-loading" role="status"><span class="loading-spinner" aria-hidden="true"></span><span>Loading Plotly figure…</span></div></div><details class="chart-table-disclosure" data-view-key="${e(id)}"><summary>Full Plotly figure specification</summary><pre class="json-view">${e(JSON.stringify(value.spec || {},null,2))}</pre></details>`;
}

/** Resolve authenticated media separately from text rendering.
 * Main callers: render and openRun. Only local object URLs returned by the API
 * are embedded. This prevents logged values from leaking credentials to URLs.
 */
async function hydrateMedia(root) {
  const charts = Promise.all([hydrateVega(root, vegaRegistry),
    hydratePlotly(root, plotlyRegistry, {getMediaUrl, mapOrigins:applicationConfig?.map_resource_origins || []})]);
  for (const element of root.querySelectorAll('[data-media]')) {
    const value = mediaRegistry.get(element.dataset.media);
    if (!value || element.dataset.mediaReady) continue;
    try {
      const url = await getMediaUrl(value);
      if (!url || !url.startsWith('blob:')) throw new Error('The media API did not return a local object URL.');
      if (!element.isConnected) continue;
      let node;
      if (value._type === 'graph' || value._type === 'graph-file') {
        const response = await fetch(url); if (!response.ok) throw new Error('Could not read logged graph.');
        node = graphViewer(parseGraphJSON(await response.text()), value);
      } else if (value._type === 'html') {
        const response = await fetch(url); if (!response.ok) throw new Error('Could not read logged HTML.');
        const content = await response.text();
        node = document.createElement('iframe'); node.setAttribute('sandbox', ''); node.setAttribute('referrerpolicy', 'no-referrer'); node.title = value.caption || 'Logged HTML'; node.className = 'html-frame'; node.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src blob: data:; media-src blob:; font-src data:; base-uri 'none'; form-action 'none'">${content}`;
      } else if (value._type === 'image') {
        node = await imageViewer(value, url, getMediaUrl);
      } else {
        node = document.createElement(value._type === 'video' && (value.mime_type === 'image/gif' || value.format === 'gif') ? 'img' : value._type);
        watchMediaErrors(node, element, value, url);
        node.src = url; node.className = 'rich-media';
        if (node.tagName === 'IMG') { node.alt = value.caption || 'Logged image'; node.loading = 'lazy'; }
        else { node.controls = true; node.preload = 'metadata'; }
      }
      if (element.isConnected) { element.replaceChildren(node); element.dataset.mediaReady = 'true'; }
    } catch (error) { if (element.isConnected) { element.className = 'notice error'; element.textContent = `Could not load ${value._type}: ${error.message}`; } }
  }
  await charts;
}

function renderSetup() {
  const install = "python -m pip install 'backprop-foundry @ git+https://github.com/tginart/backprop.git@v0.1.1'";
  const project = chosenProject()?.name || 'my-project';
  const code = `import backprop as wandb\n\nwith wandb.init(project=${JSON.stringify(project)},\n                config={"learning_rate": 3e-4}) as run:\n    for step in range(100):\n        run.log({"train/loss": 1 / (step + 1)}, step=step)`;
  const env = 'export BACKPROP_CONFIG="/path/to/backprop-config.json"\nexport FOUNDRY_TOKEN="<your Foundry token>"';
  return `<div class="onboarding"><div class="onboarding-intro"><div class="eyebrow">Python to Foundry</div><h1>Your training loop. A new perspective.</h1><p>A familiar logging interface, connected directly to Foundry.<br>Start with a run, log metrics, and watch this workspace update.</p></div><section class="setup-card"><header><span class="step-number">1</span><h3>Install the Python package</h3><button class="icon-btn copy" data-copy="${e(install)}" aria-label="Copy install command">${icon('copy')}</button></header><pre>${e(install)}</pre><p>Requires Python 3.10 or newer. See the <a href="https://github.com/tginart/backprop#quick-start" target="_blank" rel="noopener noreferrer">Quick Start</a> for installation and optional integrations.</p></section><section class="setup-card"><header><span class="step-number">2</span><h3>Configure your Foundry connection</h3><button class="icon-btn copy" data-copy="${e(env)}" aria-label="Copy environment variables">${icon('copy')}</button></header><pre>${e(env)}</pre><p><button class="btn small" data-action="download-config">${icon('download')} Download connection file</button></p><p>Save the connection file, then set BACKPROP_CONFIG to its path. It contains dataset IDs and no credentials. Keep tokens in your environment.</p></section><section class="setup-card"><header><span class="step-number">3</span><h3>Log an experiment</h3><button class="icon-btn copy" data-copy="${e(code)}" aria-label="Copy logging example">${icon('copy')}</button></header><pre>${e(code)}</pre><p>Backprop supports scalar metrics, config, summary, custom step axes, tables, histograms, plots, and logged media. The README documents the exact compatibility surface.</p></section><button class="btn primary" data-view="workspace">Open workspace ${icon('arrow')}</button></div>`;
}

function renderPinnedConfig(run) {
  const values = new Map(flatten(run.config));
  const keys = prioritizeConfigKeys([...values.keys()], run.pinned_config_keys).filter(key => isPinnedConfigKey(key, run.pinned_config_keys));
  if (!keys.length) return '';
  return `<section class="artifact-card"><header><h3>${icon('pin')} Pinned configuration</h3></header>${pathNote([...values.keys()])}<div class="table-wrap"><table class="kv-table"><tbody>${keys.map(key => `<tr><td data-pinned-config="true">${e(key)}</td><td>${valueCell(values.get(key))}</td></tr>`).join('')}</tbody></table></div></section>`;
}

/** Dedicated run navigation shares recorded content with quick inspection.
 * Main callers: run-name buttons, bookmarked URLs and the run tab bar.
 * Why this exists: run analysis needs a stable URL and full canvas, while quick
 * inspection deliberately pauses refresh in a modal over the project view.
 */
const RUN_TABS = [['charts','Charts'],['overview','Overview'],['console','Logs'],['files','Files'],['artifacts','Artifacts']];
function openRunPage(key, tab = 'charts') {
  const run=model.runs.find(item=>item.key===key);if(!run)return;
  if(state.view!=='run')state.runReturn={view:state.view,page:state.runPage,snapshot:captureViewState(app)};
  state.project=projectKey(run);state.runKey=key;state.runTab=tab;state.view='run';
  updateLink(true);render();
}
function renderRunPage() {
  const choices=analysis(), runs=selectAnalysisRuns(projectRuns(),{sort:choices.sort,direction:choices.direction}), run=runs.find(item=>item.key===state.runKey);
  if(!run)return '<div class="empty"><h2>Run unavailable.</h2><p>This run is absent from the current project snapshot.</p><button class="btn" data-view="workspace">Back to workspace</button></div>';
  const position=runs.indexOf(run), previous=runs[position-1], next=runs[position+1];
  const tab=RUN_TABS.some(([key])=>key===state.runTab)?state.runTab:'charts';
  const content=tab==='charts' ? renderWorkspace([run]) : renderRunContent(run,tab);
  return `<article class="run-page"><button class="btn small exit-run" data-action="exit-run">${icon('left')} Exit single-run view</button><div class="breadcrumb"><button class="text-button" data-view="workspace">${e(run.project)}</button><span>/</span><span>Runs</span><span>/</span><strong>${e(run.id)}</strong></div><div class="page-heading"><div><h1><span class="dot" style="background:${run.color}"></span>${e(run.name)}</h1><p>${stateBadge(run)} · ${e(run.group || 'No group')} · Created ${e(compactDate(run.created_at))}</p></div><div class="heading-actions"><button class="icon-btn" data-copy="${e(location.href)}" aria-label="Copy run page link">${icon('copy')}</button><button class="icon-btn" data-run="${e(previous?.key || '')}" ${previous?'':'disabled'} aria-label="Previous run">${icon('left')}</button><button class="icon-btn" data-run="${e(next?.key || '')}" ${next?'':'disabled'} aria-label="Next run">${icon('chevron')}</button></div></div><nav class="view-tabs run-tabs" aria-label="Run details">${RUN_TABS.map(([key,label])=>`<button class="view-tab ${tab===key?'active':''}" data-run-tab="${key}" ${tab===key?'aria-current="page"':''}>${label}</button>`).join('')}</nav><div class="run-page-content">${content}</div><div class="detail-freshness">Loaded ${e(fullDate(state.fetchedAt))}. Display settings do not change logged values.</div></article>`;
}

function renderRunContent(run, tab) {
  let content;
  if (tab === 'overview') content = `<div class="detail-overview">${[['State', run.state], ['Run ID', run.id], ['Project', run.project], ['Created', fullDate(run.created_at)], ['Last event', fullDate(run.updated_at)], ['Group', run.group || 'None'], ['Job type', run.job_type || 'None'], ['History rows', run.history.length], ['Events', run.eventCount]].map(([label, value]) => `<div><label>${e(label)}</label><p>${e(value)}</p></div>`).join('')}</div>${renderPinnedConfig(run)}${run.tags.length ? `<p style="margin-bottom:20px">${run.tags.map(tag => `<span class="tag">${e(tag)}</span>`).join(' ')}</p>` : ''}${run.notes ? `<pre class="json-view">${e(run.notes)}</pre>` : ''}<button class="btn" data-focus-run="${e(run.key)}">${icon('pulse')} Show this run's metrics</button>`;
  else if (tab === 'console') content = run.console.length ? renderConsole(run.console) : '<div class="empty"><h2>No console messages.</h2><p>Messages sent with run.log_console() appear here.</p></div>';
  else if (tab === 'files' || tab === 'artifacts') content = renderRunFiles(run,tab);
  else { const data = tab === 'config' ? run.config : run.summary, values = new Map(flatten(data)), pins = tab === 'config' ? run.pinned_config_keys : []; content = `${pathNote([...values.keys()])}<div class="table-wrap"><table class="kv-table"><thead><tr><th>Key</th><th>Value</th></tr></thead><tbody>${prioritizeConfigKeys([...values.keys()], pins).map(key => `<tr><td ${isPinnedConfigKey(key, pins) ? 'data-pinned-config="true" title="Pinned configuration"' : ''}>${isPinnedConfigKey(key, pins) ? icon('pin') + ' ' : ''}${e(key)}</td><td>${tab === 'summary' && (isRich(values.get(key)) || Array.isArray(values.get(key))) ? renderRichValue(values.get(key), `summary:${run.key}:${key}`, run.color) : valueCell(values.get(key))}</td></tr>`).join('') || '<tr><td colspan="2" class="dim">No values recorded.</td></tr>'}</tbody></table></div>`; }
  if(tab==='overview')content+=`<section class="run-overview-values"><h2>Configuration</h2>${renderRunContent(run,'config')}<h2>Summary</h2>${renderRunContent(run,'summary')}</section>`;
  if (tab === 'overview' && Object.keys(run.writers || {}).length) content += `<section class="artifact-card"><h3>Shared writers</h3><p class="dim">Writer steps are independent. History stays incomplete while a writer is running or earlier records have not become visible.</p><div class="table-wrap"><table class="kv-table shared-writer-table"><thead><tr><th>Writer</th><th>Role</th><th>Recorded state</th><th>History</th></tr></thead><tbody>${Object.values(run.writers).map(writer => `<tr><td data-label="Writer">${e(writer.label || writer.id)}</td><td data-label="Role">${writer.primary?'Primary':'Contributor'}</td><td data-label="Recorded state">${e(writer.state)}</td><td data-label="History">${e(completionText((run.history_incomplete_writers || []).find(item => item.id === writer.id)))}</td></tr>`).join('')}</tbody></table></div></section>`;
  return content;
}

function openRun(key, tab = 'overview') {
  const run = model.runs.find(run => run.key === key); if (!run) return;
  const focus = rememberFocus(detailDialog);
  state.dialogTab = tab; detailDialog.dataset.run = key;
  const tabs = ['overview', 'config', 'summary', 'console', 'files'];
  const content = tab === "files" ? renderRunFiles(run) : renderRunContent(run,tab);
  const position = projectRuns().findIndex(item => item.key === key), previous = projectRuns()[position - 1], next = projectRuns()[position + 1];
  const link = new URL(location.href); link.searchParams.delete('entity'); link.searchParams.set('project', projectKey(run)); link.searchParams.set('run', run.id); link.searchParams.set('view', 'run');
  replaceView(detailDialog, `<header class="dialog-header"><div class="detail-title"><span class="dot" style="background:${run.color}"></span><div><div class="eyebrow">${e(run.project)} / ${e(run.id)}</div><h2>${e(run.name)}</h2></div></div><div class="detail-actions"><button class="icon-btn" data-copy="${e(link.href)}" aria-label="Copy run link" title="Copy run link">${icon('copy')}</button><button class="icon-btn" data-run="${e(previous?.key || '')}" ${previous ? '' : 'disabled'} aria-label="Previous run">${icon('left')}</button><button class="icon-btn" data-run="${e(next?.key || '')}" ${next ? '' : 'disabled'} aria-label="Next run">${icon('chevron')}</button><button class="icon-btn" data-action="close-dialog" aria-label="Close run details">${icon('close')}</button></div></header><div class="dialog-body"><nav class="dialog-tabs">${tabs.map(name => `<button class="dialog-tab ${tab === name ? 'active' : ''}" data-detail-tab="${name}">${name[0].toUpperCase() + name.slice(1)}</button>`).join('')}</nav><div class="detail-freshness"><span>Updates paused while inspecting · Loaded <time title="${e(fullDate(state.fetchedAt))}">${e(fullDate(state.fetchedAt))}</time></span><button class="btn small" data-action="refresh-detail" ${state.refreshing ? 'disabled' : ''}>${icon('refresh')} ${state.refreshing ? 'Refreshing' : 'Refresh view'}</button></div>${state.error ? `<div class="notice error" role="alert">${e(state.error)} Showing the previous snapshot.</div>` : ''}${content}${tab === 'overview' && (run.control?.stop_supported && run.state === 'running' || run.stop_requests?.length) ? `<div class="run-control-entry"><span class="muted">${run.stop_requests?.length ? 'A stop request is recorded for this process.' : 'This process supports remote stop requests.'}</span><button class="btn small ${run.stop_requests?.length ? '' : 'danger'}" data-stop-run="${e(run.key)}">${run.stop_requests?.length ? 'Stop request status' : 'Stop run'}</button></div>` : ''}</div>`);
  if (!detailDialog.open) detailDialog.showModal();
  bindCharts(detailDialog); hydrateMedia(detailDialog); restoreFocus(detailDialog, focus);
}

/** Show immutable model/data inputs independently of their output manifests.
 * Main caller: renderRunFiles. Repeated use calls form one visible relationship;
 * source identity remains useful when its manifest is outside this workspace.
 */
function renderArtifactInputs(run) {
  const inputs = new Map();
  for (const [index, item] of (run.used_artifacts || []).entries()) {
    const key = JSON.stringify([item.source_entity ?? run.entity, item.source_project ?? run.project, item.artifact_id ?? ["unidentified",index], item.version ?? null]);
    if (!inputs.has(key)) inputs.set(key, item);
  }
  if (!inputs.size) return '';
  return `<section class="artifact-card input-artifacts"><header><h3>Input artifacts</h3><span class="tag">${inputs.size}</span></header><p class="dim">Artifacts used by this run.</p><div class="table-wrap"><table><thead><tr><th>Name</th><th>Type</th><th>Version</th><th>Source</th></tr></thead><tbody>${[...inputs.values()].map(item => {const version=typeof item.version==='number'?`v${item.version}`:item.version || '',name=item.name || item.artifact_id || 'Unidentified artifact';return `<tr><td title="${e(item.qualified_name || name)}">${e(version && name.endsWith(':'+version)?name.slice(0,-(version.length+1)):name)}</td><td>${e(item.artifact_type || 'artifact')}</td><td><span class="badge artifact-version" title="${e(version)}">${e(version || 'Not recorded')}</span></td><td>${e([item.source_entity ?? run.entity, item.source_project ?? run.project].filter(Boolean).join('/'))}</td></tr>`;}).join('')}</tbody></table></div></section>`;
}

function renderRunFiles(run, mode = "both") {
  const filesTable = (files, artifact) => `<div class="table-wrap"><table><thead><tr><th>File</th><th>Size</th><th>Integrity</th><th></th></tr></thead><tbody>${files.map(file => { const id = `file-${++mediaSequence}`; mediaRegistry.set(id, file); if(file._backprop_external_reference)return `<tr><td class="mono">${e(file.name || file.path)}<code class="external-reference-uri">${e(file.uri || file.ref || '')}</code></td><td>${file.size===undefined?'Unknown':`${f(file.size/1024)} KB`}</td><td class="dim">Provider reference</td><td>Use the Python SDK with provider credentials.</td></tr>`; if (artifact && isArtifactTable(file)) artifactTableRegistry.set(id, { artifact, path: file.name }); return `<tr><td class="mono">${e(file.name || file.path)}</td><td>${file.size===undefined?'Unknown':`${f(file.size / 1024)} KB`}</td><td class="mono dim" title="${e(file.sha256 || file.digest || '')}">${e((file.sha256 || file.digest || '').slice(0, 12) || 'Not recorded')}</td><td class="file-actions">${artifact && isArtifactTable(file) ? `<button class="btn small" data-artifact-table="${id}">${icon('data')} Open table</button>` : ''}<button class="btn small" data-download-file="${id}">${icon('download')} Download</button></td></tr>`; }).join('')}</tbody></table></div>`;
  const pending = (run.pending_artifacts || []).length ? `<section class="artifact-card"><h3>Pending artifact contributions</h3><p class="dim">These uploaded parts have not appeared in a finalized manifest. Other writers may still be uploading.</p>${run.pending_artifacts.map(part => `<p><span class="tag">Incomplete</span> ${e(part.manifest.name)} · ${e(part.distributed_id)} · ${part.manifest.files?.length || 0} files</p>`).join('')}</section>` : '';
  const artifacts = run.artifacts.map(artifact => `<section class="artifact-card"><header><h3>${e(artifact.name)}</h3><span class="tag">${e(artifact.artifact_type || 'artifact')}</span>${artifact.version !== undefined ? `<span class="badge artifact-version" title="${e(artifact.version)}">${e(typeof artifact.version === 'number' ? `v${artifact.version}` : artifact.version)}</span>` : ''}<span class="dim">${e(artifact.state || '')}</span></header>${artifact.aliases?.length ? `<p>${artifact.aliases.map(alias => `<span class="tag artifact-alias" title="${e(alias === '' ? 'Empty alias' : alias)}">${e(alias === '' ? '""' : alias)}</span>`).join(' ')}</p>` : ''}${artifact.description ? `<p class="artifact-description">${e(artifact.description)}</p>` : ''}<div class="artifact-meta"><span>${artifact.files?.length || 0} files</span><span>${e(fullDate(artifact.timestamp))}</span>${artifact.digest ? `<span class="mono" title="${e(artifact.digest)}">Digest ${e(artifact.digest.slice(0, 16))}</span>` : ''}</div>${filesTable(artifact.files || [], artifact)}${artifact.metadata && Object.keys(artifact.metadata).length ? `<details class="chart-table-disclosure"><summary>Artifact metadata</summary><pre class="json-view">${e(JSON.stringify(artifact.metadata, null, 2))}</pre></details>` : ''}</section>`).join('');
  return (mode !== 'artifacts' && run.files.length ? `<section class="artifact-card"><header><h3>Run files</h3><span class="tag">${run.files.length}</span></header>${filesTable(run.files)}</section>` : '') + (mode !== 'files' ? renderArtifactInputs(run) + pending + artifacts : '') || `<div class="empty"><h2>No ${mode === 'artifacts' ? 'artifacts' : 'files'} logged.</h2><p>${mode === 'artifacts' ? 'Immutable artifact manifests appear here.' : 'Files saved to this run appear here.'}</p></div>`;
}

const chartRuns = () => state.view === 'run' ? projectRuns().filter(run=>run.key===state.runKey) : visibleRuns();
function expandChart(metric) {
  expandedChartObserver.disconnect();
  chartDialog.innerHTML = `<header class="dialog-header"><h2>${e(panelTitle(metric))}</h2><button class="icon-btn" data-action="close-dialog" aria-label="Close expanded chart">${icon('close')}</button></header><div class="dialog-body">${renderMetricCard(metric, chartRuns(), true)}<p class="table-note">Drag over the chart to zoom. Double click to reset. Hover to inspect values.</p></div>`;
  if (!chartDialog.open) chartDialog.showModal();
  bindCharts(chartDialog);hydrateMedia(chartDialog);
  const plot = chartDialog.querySelector('[data-chart]');
  if (plot) expandedChartObserver.observe(plot);
}

function download(content, filename, type = 'text/csv') {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const fileName = value => value.replace(/[^a-z0-9._-]/gi, '_').slice(0, 120);
function exportMetric(metric) {
  const { series, options, error } = metricPanel(metric, chartRuns());
  if(error)throw new Error(error);
  const rows = [['series', 'step', options.xLabel, 'raw_value', 'displayed_value']];
  for (const item of series) for (const point of item.points) rows.push([item.name, point.step, point.x, point.raw, point.y]);
  download(csv(rows), `${fileName(metric)}.csv`); toast('Chart data exported.');
}

/** Fetch new events, rebuild the model, and keep prior data if the network fails.
 * Main callers: boot, 30-second interval, refresh button. Only one request runs
 * at a time so stream offsets cannot race.
 */
async function refresh({ refreshArchive = false } = {}) {
  if (request || !getAuthState().authenticated) return;
  state.refreshing = true;
  for (const button of app.querySelectorAll('[data-action="refresh"]')) button.disabled = true;
  request = new AbortController();
  try {
    const workspace = await withDeadline(signal => readWorkspace({signal, refreshArchive}), {signal:request.signal, timeoutMs:240000});
    model = normalizeEvents(workspace.events || []);
    projectSourceCache.clear();renderedCatalog=null;
    state.warning = workspace.warning || '';
    state.fetchedAt = workspace.fetchedAt || new Date().toISOString();
    state.source = workspace.source || 'Foundry stream';
    state.error = '';
    if (!appliedRunLink) {
      const query = new URLSearchParams(location.search), projectName = query.get('project');
      const linked = model.projects.find(project => (project.id === projectName || project.name === projectName) && (!query.get('entity') || project.entity === query.get('entity')));
      if (linked) state.project = linked.id;
    }
    if (!model.projects.some(project => project.id === state.project)) state.project = model.projects.find(project => project.name === 'backprop-demo')?.id || model.projects[0]?.id || '';
    initializeSelection();
    if (!appliedRunLink) {
      const runId = new URLSearchParams(location.search).get('run'), linked = projectRuns().find(run => run.id === runId);
      if (linked && new URLSearchParams(location.search).get('view') !== 'run') { state.runKey=linked.key;state.runTab='charts';state.view='run'; }
      const linkedView = new URLSearchParams(location.search).get('view');
      state.artifactSelection=Object.fromEntries(['collection','version','artifactTab'].filter(key=>new URLSearchParams(location.search).has(key)).map(key=>[key==='artifactTab'?'tab':key,new URLSearchParams(location.search).get(key)]));
      if (['overview','artifacts','workspace', 'runs', 'compare', 'data', 'setup','run'].includes(linkedView)) state.view = linkedView;
      if (linked && linkedView==='run') {state.runKey=linked.key;const tab=new URLSearchParams(location.search).get('tab');state.runTab=RUN_TABS.some(([key])=>key===tab)?tab:'charts';}
      appliedRunLink = true;
    }
    const displayEvents=workspace.events.filter(isWorkspaceEvent);
    if(workspaceSession)workspaceSession.ingest(displayEvents);
    else if(CUSTOM_WORKSPACES && chosenProject())await openWorkspaceView(new URLSearchParams(location.search).get('nw') || state.activeViews.get(state.project) || 'personal',{events:displayEvents,seed:currentSnapshot()});
  } catch (error) { state.auth = getAuthState(); state.error = error.message || 'Could not load the Foundry workspace.'; }
  finally {
    request = null; state.loading = false; state.refreshing = false; state.initialized = true;
    for (const button of app.querySelectorAll('[data-action="refresh"]')) button.disabled = false;
    refreshRenderer.request(); persist();
  }
}

function initializeSelection() {
  // Newly arriving runs are visible by default. Only explicit eye overrides persist.
  const keys=new Set(model.runs.map(run=>run.key));
  for(const key of state.selected)if(!keys.has(key))state.selected.delete(key);
}

async function boot() {
  state.error = '';
  try {
    applicationConfig = await initialize();
    state.auth = getAuthState();
    state.loading = state.auth.authenticated;
    render();
    if (state.auth.authenticated) {
      await refresh();
      const url=new URL(location.href);
      if(url.searchParams.has('panel')) {const metric=url.searchParams.get('panel');url.searchParams.delete('panel');history.replaceState({},'',url);if(panelCatalog().has(metric))expandChart(metric);else toast('This panel has no data in the current run selection.');}
      if(url.searchParams.get('control')==='stop') {
        const run=projectRuns().find(run=>run.id===url.searchParams.get('run'));
        url.searchParams.delete('control'); history.replaceState({},'',url);
        if(run) { openRun(run.key); runControl.open(run); }
      }
    }
    refreshTimer = setInterval(() => { if (state.live && document.visibilityState === 'visible' && !document.querySelector('dialog[open]')) refresh(); }, 30000);
  } catch (error) { state.loading = false; state.error = error.message || 'Backprop could not initialize.'; render(); }
}

document.addEventListener('click', async event => {
  const target = event.target.closest('button, [data-action]'); if (!target) return;
  try {
    if(target.dataset.artifactCollection){state.artifactSelection={collection:target.dataset.artifactCollection,tab:'version'};updateLink(true);render();return;}
    if(target.dataset.artifactTab){state.artifactSelection.tab=target.dataset.artifactTab;updateLink(true);render();return;}
    if (target.dataset.sectionMenu) {sectionMenu(target,target.dataset.sectionMenu);return;}
    if (target.dataset.panelMenu) {panelMenu(target,target.dataset.panelMenu);return;}
    if (target.dataset.sectionAddPanel) {addPanelToSection(target.dataset.sectionAddPanel);return;}
    if (target.dataset.sectionPin) {const id=target.dataset.sectionPin;setSection(layout(),id,{pinned:!sectionOptions(layout(),id).pinned});persist();render();return;}
    if (target.dataset.sectionPage) {const id=target.dataset.sectionPage;setSection(layout(),id,{page:Math.max(0,(sectionOptions(layout(),id).page || 0)+Number(target.dataset.pageDelta))});persist();render();return;}
    if (target.dataset.pinRun) {const pins=new Set(layout().pinnedRuns || []);pins.has(target.dataset.pinRun)?pins.delete(target.dataset.pinRun):pins.add(target.dataset.pinRun);layout().pinnedRuns=[...pins];persist();render();return;}
    if (target.dataset.runVisibility) { const key=target.dataset.runVisibility;state.hiddenRuns.has(key)?state.hiddenRuns.delete(key):state.hiddenRuns.add(key);persist();render();return; }
    if (target.dataset.view) { state.view = target.dataset.view; updateLink(true); render(); return; }
    if (target.dataset.editPanel) { openPanel(target.dataset.editPanel); return; }
    if (target.dataset.pin) { const key = panelKey(state.project, target.dataset.pin); state.pinnedPanels.has(key) ? state.pinnedPanels.delete(key) : state.pinnedPanels.add(key); persist(); render(); if (inspectDialog.open && inspectDialog.classList.contains('panel-manager')) managePanels(); return; }
    if (target.dataset.section) { const key = panelKey(state.project, target.dataset.section); state.collapsedSections.has(key) ? state.collapsedSections.delete(key) : state.collapsedSections.add(key); persist(); render(); return; }
    if (target.dataset.legendMetric) { const key = panelKey(state.project, target.dataset.legendMetric), hidden = state.hiddenSeries.get(key) || new Set(); hidden.has(target.dataset.series) ? hidden.delete(target.dataset.series) : hidden.add(target.dataset.series); state.hiddenSeries.set(key, hidden); chartDialog.open ? expandChart(target.dataset.legendMetric) : render(); return; }
    if (target.dataset.artifactSnapshot) { const source = tableRegistry.get(target.dataset.artifactSnapshot), snapshot = source?.artifact_snapshot; if (snapshot) await tableExplorer.openSnapshot(snapshot, (snapshot.source_log_mode || source.source_log_mode || source.log_mode) === 'INCREMENTAL' ? 'Full artifact snapshot · this increment' : 'Full artifact snapshot'); return; }
    if (target.dataset.artifactTable) { const entry = artifactTableRegistry.get(target.dataset.artifactTable); if (entry) await tableExplorer.openArtifact(entry.artifact, entry.path); return; }
    if (target.dataset.linkedTable) { const id = target.dataset.linkedTable, table = tableRegistry.get(id), context = tableContexts.get(id); if (table && context) await tableExplorer.follow({ id, table, context, title: context.title }, Number(target.dataset.row), Number(target.dataset.column), target.dataset.relationIndex === undefined ? undefined : Number(target.dataset.relationIndex)); return; }
    if (target.dataset.inspectSchema) { openColumnType(target.dataset.inspectSchema, Number(target.dataset.column)); return; }
    if (target.dataset.inspectTable) { openTableRow(target.dataset.inspectTable, Number(target.dataset.row)); return; }
    if (target.dataset.tableColumns) { openTableColumns(target.dataset.tableColumns); return; }
    if (target.dataset.tableSort) { const id = target.dataset.tableSort, options = state.tableOptions.get(id) || {}, column = Number(target.dataset.column); state.tableOptions.set(id, { ...options, sort: column, direction: options.sort === column ? -(options.direction || 1) : 1 }); state.tablePages.set(id, 0); persist(); refreshTableView(id); return; }
    if (target.dataset.exportTable) { const id = target.dataset.exportTable, table = tableRegistry.get(id), options = state.tableOptions.get(id) || {}; const columns = table.columns.map((name, index) => ({name, index})).filter(({index}) => !options.hiddenColumns?.includes(index)); download(csv([columns.map(col => col.name), ...tableRows(table, { ...options, search: tableFilters.get(id) || '' }).map(({cells}) => columns.map(col => typeof cells[col.index] === 'object' ? JSON.stringify(cells[col.index]) : cells[col.index]))]), 'table-view.csv'); return; }
    if (target.dataset.richId) { target.dataset.historyStep === '' ? state.richSteps.delete(target.dataset.richId) : state.richSteps.set(target.dataset.richId, target.dataset.historyStep); persist(); render(); return; }
    if (target.dataset.stopRun) { const run=model.runs.find(run=>run.key===target.dataset.stopRun); if(run) runControl.open(run); return; }
    if (target.dataset.panelLayout) {openPanelLayout(target.dataset.panelLayout);return;}
    if (target.dataset.sectionSettings) {openSectionSettings(target.dataset.sectionSettings);return;}
    if (target.dataset.inspectRun) {openRun(target.dataset.inspectRun);return;}
    if (target.dataset.runTab) {state.runTab=target.dataset.runTab;updateLink(true);render();return;}
    if (target.dataset.run) {detailDialog.open ? openRun(target.dataset.run,state.dialogTab) : openRunPage(target.dataset.run,state.view==='run'?state.runTab:'charts');return;}
    if (target.dataset.detailTab) { openRun(detailDialog.dataset.run, target.dataset.detailTab); return; }
    if (target.dataset.focusRun) { detailDialog.close();openRunPage(target.dataset.focusRun);return; }
    if (target.dataset.expand) { expandChart(target.dataset.expand); return; }
    if (target.dataset.resetChart) { state.zoom.delete(target.dataset.resetChart); chartDialog.open ? expandChart(target.dataset.resetChart) : render(); return; }
    if (target.dataset.exportMetric) { exportMetric(target.dataset.exportMetric); return; }
    if (target.dataset.exportRich) { const value = richRegistry.get(target.dataset.exportRich); value?._type === 'table' ? download(csv([value.columns, ...value.data]), `${fileName(target.dataset.exportRich)}.csv`) : download(JSON.stringify(value, null, 2), `${fileName(target.dataset.exportRich)}.json`, 'application/json'); return; }
    if (target.dataset.downloadFile) { const file = mediaRegistry.get(target.dataset.downloadFile), url = await getMediaUrl(file); if (!url.startsWith('blob:')) throw new Error('The file API returned an unexpected URL.'); const link = document.createElement('a'); link.href = url; link.download = file.name || 'download'; link.click(); return; }
    if (target.dataset.sort) { const choices = analysis(); if (choices.sort === target.dataset.sort) choices.direction *= -1; else { choices.sort = target.dataset.sort; choices.direction = 1; } persist(); render(); return; }
    if (target.dataset.page) { state.runPage += Number(target.dataset.page); render(); return; }
    if (target.dataset.tablePage) { const key = target.dataset.tablePage; state.tablePages.set(key, (state.tablePages.get(key) || 0) + Number(target.dataset.delta)); refreshTableView(key); return; }
    if (target.dataset.copy) { await navigator.clipboard.writeText(target.dataset.copy); toast('Copied to clipboard.'); return; }
    switch (target.dataset.action) {
      case 'login': state.loading = true; render(); await login(); break;
      case 'logout': await logout(); model = { runs: [], projects: [], issues: [], eventCount: 0 }; render(); break;
      case 'refresh': if (!applicationConfig) await boot(); else await refresh({ refreshArchive: true }); break;
      case 'refresh-detail': {
        const key = detailDialog.dataset.run, tab = state.dialogTab, body = detailDialog.querySelector('.dialog-body'), scroll = body?.scrollTop || 0;
        const pending = refresh({ refreshArchive: true }); openRun(key, tab); await pending;
        if (detailDialog.open && detailDialog.dataset.run === key && state.dialogTab === tab) {
          const restoreButton = document.activeElement === document.body || document.activeElement === detailDialog;
          openRun(key, tab); detailDialog.querySelector('.dialog-body').scrollTop = scroll;
          if (restoreButton) detailDialog.querySelector('[data-action="refresh-detail"]').focus({preventScroll:true});
        }
        break;
      }
      case 'exit-run': { const previous=state.runReturn;state.view=previous?.view || 'workspace';state.runPage=previous?.page || 0;updateLink(true);render({restore:previous?.snapshot});break;}
      case 'live': state.live = !state.live; render(); if (state.live) refresh({ refreshArchive: true }); break;
      case 'show-all-runs': for(const run of filteredRuns())state.hiddenRuns.delete(run.key);persist();render();break;
      case 'hide-all-runs': for(const run of filteredRuns())state.hiddenRuns.add(run.key);persist();render();break;
      case 'visibility-menu': openActionMenu(target,[{label:'Show all matching runs',run:()=>{for(const run of filteredRuns())state.hiddenRuns.delete(run.key);persist();render();}},{label:'Hide all matching runs',run:()=>{for(const run of filteredRuns())state.hiddenRuns.add(run.key);persist();render();}}],toast);break;
      case 'show-selected-runs': for(const run of selectedRuns())state.hiddenRuns.delete(run.key);persist();render();break;
      case 'hide-selected-runs': for(const run of selectedRuns())state.hiddenRuns.add(run.key);persist();render();break;
      case 'clear-row-selection': state.selected.clear();render();break;
      case 'select-all': { const runs = filteredRuns(), all = runs.every(run => state.selected.has(run.key)); for (const run of runs) all ? state.selected.delete(run.key) : state.selected.add(run.key); updateLink(); render(); break; }
      case 'clear-data-filters': state.richRun = ''; state.richType = ''; state.richSearch = ''; render(); break;
      case 'clear-metrics': state.metricSearch = ''; render(); break;
      case 'workspace-menu': openActionMenu(target,[{label:'Copy workspace URL',run:async()=>{const url=new URL(location.href);url.searchParams.delete('panel');await navigator.clipboard.writeText(url.href);toast('Workspace link copied. Other browsers load the saved version.');}},{label:'Save as new view',run:()=>saveView(true)},{label:'Reset chart layout',run:()=>{const previous=layout();state.workspaceLayouts.set(state.project,{...newLayout(),sidebarWidth:previous.sidebarWidth,sidebarHidden:previous.sidebarHidden,pinnedColumns:previous.pinnedColumns,pinnedRuns:previous.pinnedRuns,runPageSize:previous.runPageSize});for(const collection of [state.hiddenPanels,state.pinnedPanels,state.collapsedSections])for(const key of collection)if(JSON.parse(key)[0]===state.project)collection.delete(key);persist();render();toast('Chart layout reset. Saved views and run selection are retained.');}}],toast);break;
      case 'panel-regex': state.metricRegex=!state.metricRegex;persist();render();break;
      case 'run-regex': analysis().searchRegex=!analysis().searchRegex;state.runPage=0;persist();render();break;
      case 'hide-run-sidebar': layout().sidebarHidden=true;persist();render();document.querySelector('[data-action="show-run-sidebar"]')?.focus();break;
      case 'show-run-sidebar': layout().sidebarHidden=false;persist();render();document.querySelector('[data-action="hide-run-sidebar"]')?.focus();break;
      case 'expand-run-table': state.view='runs';updateLink(true);render();break;
      case 'collapse-run-table': layout().sidebarHidden=false;state.view='workspace';updateLink(true);render();break;
      case 'navigation': state.compactNavigation = !state.compactNavigation; persist(); render(); break;
      case 'manage-panels': managePanels(); break;
      case 'add-panels': openPanelLibrary(); break;
      case 'run-columns': manageRunColumns(); break;
      case 'run-filters': openRunFilters({dialog:inspectDialog, runs:projectRuns(), filters:analysis().filters, onApply:filters => { analysis().filters = filters; state.runPage = 0; persist(); render(); }}); break;
      case 'run-grouping': openAnalysisSettings('grouping'); break;
      case 'run-sort': openAnalysisSettings('sort'); break;
      case 'add-section': openSectionSettings(`custom:${crypto.randomUUID()}`,true); break;
      case 'workspace-settings': openWorkspaceSettings();break;
      case 'save-view': await saveView(false); break;
      case 'undo-view': workspaceSession?.undo();break;
      case 'redo-view': workspaceSession?.redo();break;
      case 'clear-view-changes': workspaceSession?.discard();break;
      case 'reload-view': if(workspaceSession)await workspaceSession.reload();else await openWorkspaceView(state.activeViews.get(state.project)||'personal');break;
      case 'adopt-saved-view': await workspaceSession?.adoptLatest();break;
      case 'resolve-view': await workspaceSession?.save({resolveConflict:true});break;
      case 'enable-workspace-save': enableWorkspaceSaving();break;
      case 'confirm-workspace-save': persist();await login({allowWorkspaceSave:true});break;
      case 'save-view-as': await saveView(true); break;
      case 'restore-panels': for(const section of layout().sections)section.hidden=false; for (const metric of allPanels(projectRuns())) state.hiddenPanels.delete(panelKey(state.project, metric)); persist(); render(); managePanels(); break;
      case 'close-dialog': target.closest('dialog')?.close(); break;
      case 'download-config': {
        const keys = ['host', 'stream_dataset_rid', 'media_dataset_rid', 'stream_view_rid', 'branch', 'app_url'];
        const config = Object.fromEntries(keys.filter(key => applicationConfig?.[key] !== undefined).map(key => [key, applicationConfig[key]]));
        download(JSON.stringify(config, null, 2) + '\n', 'backprop-config.json', 'application/json');
        toast('Connection file downloaded. It contains no credentials.'); break;
      }
      case 'export-runs': {
        const summaries = summaryMetrics(filteredRuns()), metrics = summaries.keys;
        download(csv([['id', 'name', 'state', 'group', 'created_at', ...metrics], ...filteredRuns().map(run => [run.id, run.name, run.state, run.group, run.created_at, ...metrics.map(key => { const value = summaries.rows.get(run.key).get(key); return value?._type === 'number' ? value.value : value; })])]), `${fileName(chosenProject()?.name || 'runs')}.csv`); break;
      }
    }
  } catch (error) { state.loading = false; toast(error.message || 'The action could not be completed.'); if (target.dataset.action === 'login') { state.error = error.message; render(); } }
});

async function updateControl(event) {
  if (event.target.dataset.panelSmoothing !== undefined) { updatePanelSmoothing(event.target); persist(); return; }
  const target = event.target;
  if(target.dataset.artifactVersion!==undefined){state.artifactSelection.version=target.value;updateLink(true);render();return;}
  if(target.dataset.comparerOption){const id=panelKey(state.project,target.dataset.comparer),current=state.panelSettings.get(id)||{};state.panelSettings.set(id,{...current,[target.dataset.comparerOption]:target.type==='checkbox'?target.checked:target.value});persist();render();return;}
  if (target.dataset.viewChoice !== undefined) {await openWorkspaceView(target.value);return;}
  if (target.dataset.runColor) { if (!/^#[0-9a-f]{6}$/i.test(target.value)) { toast('Choose a valid run color.'); return; } state.runColors.set(target.dataset.runColor,target.value); persist(); render(); return; }
  if (target.dataset.analysis) { const key = target.dataset.analysis; analysis()[key] = key === 'direction' ? Number(target.value) : target.value; state.runPage = 0; state.hiddenSeries.clear(); persist(); render(); return; }
  if (target.dataset.columnSearch !== undefined) { for(const row of inspectDialog.querySelectorAll('[data-column-label]'))row.hidden=!row.dataset.columnLabel.includes(target.value.toLowerCase()); return; }
  if (target.dataset.pinColumn) { const pins=new Set(layout().pinnedColumns || []);target.checked?pins.add(target.dataset.pinColumn):pins.delete(target.dataset.pinColumn);layout().pinnedColumns=[...pins];persist();render();return; }
  if (target.dataset.runPageSize !== undefined) {layout().runPageSize=Number(target.value);state.runPage=0;persist();render();return;}
  if (target.dataset.runColumn) { const shown = new Set(runColumns().map(([key]) => key)); target.checked ? shown.add(target.dataset.runColumn) : shown.delete(target.dataset.runColumn); state.runColumns = [...shown]; layout().pinnedColumns=(layout().pinnedColumns || []).filter(key=>shown.has(key));
    const pin=target.closest('.column-choice').querySelector('[data-pin-column]');pin.disabled=!target.checked;if(!target.checked)pin.checked=false;
    persist(); render(); return; }
  if (target.dataset.panelVisible) { const key = panelKey(state.project, target.dataset.panelVisible); target.checked ? state.hiddenPanels.delete(key) : state.hiddenPanels.add(key); persist(); render(); return; }
  if (target.dataset.tableOption) { const id = target.dataset.tableOption, options = state.tableOptions.get(id) || {}; state.tableOptions.set(id, { ...options, [target.dataset.option]: target.dataset.option === 'limit' ? Number(target.value) : target.value }); state.tablePages.set(id, 0); persist(); refreshTableView(id); return; }
  if (target.dataset.columnTable) { const id = target.dataset.columnTable, options = state.tableOptions.get(id) || {}, hidden = new Set(options.hiddenColumns || []); target.checked ? hidden.delete(Number(target.value)) : hidden.add(Number(target.value)); if (hidden.size === tableRegistry.get(id).columns.length) { target.checked = true; toast('Keep at least one column visible.'); return; } state.tableOptions.set(id, { ...options, hiddenColumns: [...hidden] }); persist(); refreshTableView(id); return; }
  if (target.dataset.runSelect) { target.checked ? state.selected.add(target.dataset.runSelect) : state.selected.delete(target.dataset.runSelect); updateLink(); render(); return; }
  if (target.dataset.richStep) { target.value === '' ? state.richSteps.delete(target.dataset.richStep) : state.richSteps.set(target.dataset.richStep, target.value); persist(); render(); return; }
  if (target.dataset.tableSearch) { const id = target.dataset.tableSearch, caret = target.selectionStart; tableFilters.set(id, target.value); state.tablePages.set(id, 0); persist(); refreshTableView(id); const node = [...document.querySelectorAll('[data-table-search]')].find(node => node.dataset.tableSearch === id); node?.focus({ preventScroll: true }); node?.setSelectionRange(caret, caret); return; }
  const key = target.dataset.state; if (!key) return;
  state[key] = target.type === 'checkbox' ? target.checked : ['smoothing', 'columns'].includes(key) ? Number(target.value) : target.value;
  if (key === 'project') {if(state.view==='run')state.view='workspace';if(!CUSTOM_WORKSPACES){state.search='';state.status='';state.group='';state.metricSearch='';state.runColumns=null;state.zoom.clear();state.hiddenSeries.clear();state.richRun='';state.richType='';state.richSearch='';state.compareSearch='';}state.selected.clear();state.runPage=0;await openWorkspaceView(state.activeViews.get(state.project) || 'personal');return;}
  if (key === 'xAxis' || key === 'logScale') state.zoom.clear();
  if (key === 'search' || key === 'status' || key === 'group') state.runPage = 0;
  persist(); render();
}
document.addEventListener('input', event => {
  if (event.target.dataset.panelSmoothing !== undefined) { try { updatePanelSmoothing(event.target); } catch(error) { toast(error.message); } }
  else if (event.target.type === 'search') updateControl(event).catch(error=>toast(error.message));
  else if (event.target.dataset.state === 'smoothing') { state.smoothing = Number(event.target.value); document.querySelector('.smoothing-value').textContent = state.smoothing.toFixed(2); }
});
document.addEventListener('change', event => { if (event.target.type !== 'search') updateControl(event).catch(error=>toast(error.message)); });
document.addEventListener('keydown', event => {
  if (event.metaKey || event.ctrlKey || event.altKey || ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) || document.activeElement.isContentEditable || document.querySelector('dialog[open]')) return;
  if (event.key === '/') {
    event.preventDefault();
    // Choose by view: the run sidebar now precedes panel search in the DOM.
    const searchField = {workspace:'metricSearch', data:'richSearch', compare:'compareSearch', runs:'search'}[state.view];
    if (searchField) document.querySelector(`[data-state="${searchField}"]`)?.focus();
  }
  if (['1', '2', '3', '4'].includes(event.key)) { event.preventDefault(); state.view = ['overview', 'workspace', 'runs', 'artifacts'][Number(event.key) - 1]; updateLink(); render(); }
  if (event.key === '?') {
    inspectDialog.className = 'small-dialog';
    inspectDialog.innerHTML = `<header class="dialog-header"><h2>Keyboard shortcuts</h2><button class="icon-btn" data-action="close-dialog" aria-label="Close shortcuts">${icon('close')}</button></header><div class="dialog-body shortcut-list">${[['/','Focus search'],['1 / 2 / 3 / 4','Project / Workspace / Runs / Artifacts'],['← / →','Inspect points on a focused chart'],['Home / End','First / last chart point'],['0','Reset a focused chart'],['Escape','Close dialog or chart tooltip']].map(([key, action]) => `<p><kbd>${e(key)}</kbd><span>${action}</span></p>`).join('')}</div>`;
    inspectDialog.showModal();
  }
});
window.addEventListener('popstate',async()=>{
  const query=new URLSearchParams(location.search),project=model.projects.find(item=>item.id===query.get('project'));
  if(project)state.project=project.id;
  state.view=['overview','artifacts','workspace','runs','compare','data','setup','run'].includes(query.get('view'))?query.get('view'):'workspace';
  if(state.view==='run'){state.runKey=projectRuns().find(run=>run.id===query.get('run'))?.key || '';state.runTab=query.get('tab') || 'charts';}
  state.artifactSelection=Object.fromEntries(['collection','version','artifactTab'].filter(key=>query.has(key)).map(key=>[key==='artifactTab'?'tab':key,query.get(key)]));
  await openWorkspaceView(query.get('nw') || 'personal');render();
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && state.live && !document.querySelector('dialog[open]')) refresh({ refreshArchive: true }); });
boot();

/** Keep the project artifact tabs accessible without a pointer.
 * Main callers: native keydown in the artifact tab list. */
document.addEventListener('keydown',event=>{
  if(!event.target.matches('[data-artifact-tab]') || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();const tabs=[...event.target.closest('[role="tablist"]').querySelectorAll('[data-artifact-tab]')],index=tabs.indexOf(event.target);
  const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
  const key=tabs[next].dataset.artifactTab;tabs[next].click();document.querySelector(`[data-artifact-tab="${key}"]`)?.focus();
});
