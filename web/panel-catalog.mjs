/**
 * Architecture: all project runs -> typed logged-key catalog -> stable panel
 * definitions -> per-run snapshot lookup -> the existing scalar/rich renderers.
 * Main callers: the workspace panel bank, Add panels, and saved-view recovery.
 *
 * Product rules: source is the exact escaped logged key, while title is editable
 * display text. Discovery uses every project run, never the chart visibility
 * selection. Manual panels remain present when their source has no visible data.
 * Automatic IDs depend on type and key; custom instances have independent IDs
 * and settings. This module neither renders HTML nor writes logged values.
 */
import { definitionFor, finite, flatten, loggedValues, tableAtStep, valueAtKey } from './model.mjs';

export const PANEL_TYPES = Object.freeze([
  'line', 'table', 'image', 'audio', 'video', 'histogram', 'plot', 'plotly',
  'vega', 'html', 'graph', 'media', 'text', 'json', 'run-comparer',
]);
const types = new Set(PANEL_TYPES);
const locations = new Set(['auto', 'history', 'summary']);
const richTypes = new Set(PANEL_TYPES.filter(type => !['line', 'media', 'text', 'json'].includes(type)));
const sourceId = (type, source) => JSON.stringify([type, source]);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Supply renderer defaults without storing inherited workspace settings.
 * Main callers: Add panels and panel previews. Saved definitions store only
 * explicit overrides, so changing workspace/section defaults still propagates.
 */
export function panelDefaults(type) {
  if (!types.has(type)) throw new Error(`Unsupported panel type: ${String(type)}`);
  return type === 'line' ? {kind:'line', showRaw:true, showLegend:true} : {};
}

function valueType(value) {
  if (finite(value) || value?._type === 'number') return 'line';
  if (typeof value === 'string') return 'text';
  if (Array.isArray(value)) return value.some(item => object(item) && item._type) ? 'media' : 'json';
  if (!object(value) || typeof value._type !== 'string') return null;
  if (value._type === 'graph-file') return 'graph';
  return richTypes.has(value._type) ? value._type : 'json';
}

/** Discover Quick Add choices from recorded keys, including older rich types.
 * Main callers: materializePanels and Add panels search. History and Summary
 * share one choice when they log the same key/type; locations records provenance.
 * SDK-hidden scalar axes remain discoverable but are not automatically added.
 */
export function discoverPanelSources(runs) {
  if (!Array.isArray(runs)) throw new Error('Panel discovery requires an array of runs.');
  const sources = new Map();
  function record(run, source, value, location) {
    if (source.startsWith('_')) return;
    const type = valueType(value);
    if (!type || location === 'summary' && type === 'line') return;
    const id = sourceId(type, source);
    if (!sources.has(id)) sources.set(id, {sourceId:id, source, type, title:source, locations:new Set(), runKeys:new Set(), hidden:true});
    const entry = sources.get(id);
    entry.locations.add(location); entry.runKeys.add(run.key);
    const hidden = type === 'line' && !!definitionFor({definitions:run.definitions || {}}, source).hidden;
    entry.hidden = entry.hidden && hidden;
  }
  for (const run of runs) {
    for (const row of run.history || []) for (const [key, value] of flatten(row)) record(run, key, value, 'history');
    // The model has already accumulated incremental tables and separated writer
    // streams. This also includes Summary-only rich values and loaded descriptors.
    for (const entry of loggedValues({...run, rich:run.rich || {}, summary:run.summary || {}})) record(run, entry.key, entry.value, entry.source);
    for (const [key, value] of flatten(run.summary)) record(run, key, value, 'summary');
  }
  return [...sources.values()].map(entry => ({...entry, locations:[...entry.locations].sort(), runKeys:[...entry.runKeys],
    location:entry.locations.size > 1 ? 'auto' : [...entry.locations][0]}))
    .sort((a, b) => a.source.localeCompare(b.source) || a.type.localeCompare(b.type));
}

/** Validate one panel definition and copy its display settings.
 * Main callers: manual creation, duplication and restoring saved definitions.
 * Why this exists: a title must never double as a logged-key lookup or panel ID.
 * A missing source in today's data is allowed; malformed keys/types are errors.
 */
export function createPanel({id, type, source, title = source, location = 'auto', settings = {}, section} = {}) {
  if (!types.has(type)) throw new Error(`Unsupported panel type: ${String(type)}`);
  if (typeof source !== 'string' || !source.trim()) throw new Error('A panel source must be a nonempty logged key.');
  if (typeof title !== 'string' || !title.trim()) throw new Error('A panel title must be nonempty text.');
  if (!locations.has(location)) throw new Error(`Unsupported panel source location: ${String(location)}`);
  if (!object(settings)) throw new Error('Panel settings must be an object.');
  if (id !== undefined && (typeof id !== 'string' || !id.trim())) throw new Error('A panel ID must be nonempty text.');
  if (section !== undefined && (typeof section !== 'string' || !section.trim())) throw new Error('A panel section must be nonempty text.');
  return {id:id ?? `panel:${globalThis.crypto.randomUUID()}`, type, source, title, location,
    settings:structuredClone(settings), ...(section === undefined ? {} : {section})};
}

/** Combine saved instances with discovery, without consulting visible runs.
 * Main callers: project and single-run panel-bank rendering. Automated mode adds
 * one default panel per unrepresented source. Manual mode uses only definitions.
 * Definitions are always retained, including keys absent from the current data.
 */
export function materializePanels({runs, sources, definitions = [], mode = 'automated'} = {}) {
  if (!Array.isArray(runs) || !Array.isArray(definitions)) throw new Error('Panels require run and definition arrays.');
  if (!['automated', 'manual'].includes(mode)) throw new Error(`Unsupported workspace mode: ${String(mode)}`);
  const panels = definitions.map(definition => {
    if (typeof definition?.id !== 'string' || !definition.id.trim()) throw new Error('Saved panel definitions require stable IDs.');
    return createPanel(definition);
  });
  const ids = new Set(), represented = new Set();
  for (const panel of panels) {
    if (ids.has(panel.id)) throw new Error(`Duplicate panel ID: ${panel.id}`);
    ids.add(panel.id); represented.add(sourceId(panel.type, panel.source));
  }
  if (mode === 'automated') for (const source of sources || discoverPanelSources(runs)) {
    if (source.hidden || represented.has(source.sourceId)) continue;
    const panel = createPanel({...source, id:`auto:${source.sourceId}`});
    if (ids.has(panel.id)) throw new Error(`Duplicate panel ID: ${panel.id}`);
    panels.push({...panel, runKeys:[...source.runKeys]}); ids.add(panel.id);
  }
  return panels;
}

/** List real history snapshots for one panel source and optional writer.
 * Main callers: the panel snapshot picker and resolvePanelData. These are
 * metadata only; incremental table bytes are reconstructed when selected.
 * Summary is a latest value, so it never acquires a synthetic history step.
 */
export function panelVersions(panel, run, {writer} = {}) {
  const definition = createPanel(panel);
  if (definition.location === 'summary') return [];
  return (run.history || []).flatMap((row, index) => {
    const value = valueAtKey(row, definition.source);
    if (writer !== undefined && row._writer !== writer || valueType(value) !== definition.type) return [];
    return [{id:JSON.stringify([run.key, row._writer || '', row._step, index]), step:row._step,
      writer:row._writer, writerLabel:row._writer_label, timestamp:row._timestamp, location:'history'}];
  });
}

/** Resolve a selected rich/scalar value without guessing another step or key.
 * Main callers: panel renderers and table/media snapshot inspection. Omitted
 * step means the latest compatible history snapshot, then a Summary value when
 * allowed. Explicit missing steps produce a visible missing result. Independent
 * writer histories are replayed separately, never merged by matching step alone.
 */
export function resolvePanelData(panel, run, {step, writer} = {}) {
  const definition = createPanel(panel), versions = panelVersions(definition, run, {writer});
  const base = {panelId:definition.id, run, runKey:run.key, source:definition.source};
  const matching = step === undefined ? versions : versions.filter(version => String(version.step) === String(step));
  if (step !== undefined && writer === undefined && new Set(matching.map(version => version.writer)).size > 1)
    throw new Error('Choose a writer before selecting a shared history step.');
  const version = matching.at(-1);
  if (version) {
    const value = tableAtStep({...run, history:run.history || []}, definition.source, version.step, version.writer);
    return {...base, ...version, value, missing:false};
  }
  if (step === undefined && writer === undefined && definition.location !== 'history') {
    const value = valueAtKey(run.summary || {}, definition.source);
    if (valueType(value) === definition.type) return {...base, location:'summary', timestamp:run.updated_at, value, missing:false};
  }
  return {...base, location:definition.location, step, writer, value:undefined, missing:true,
    reason:step === undefined ? 'No compatible logged value in this run.' : 'No compatible value at the selected history step.'};
}

/** Keep missing runs in the result so panel renderers can explain empty data.
 * Main callers: rich panels spanning the workspace's visible run collection.
 */
export function resolvePanelValues(panel, runs, options = {}) {
  if (!Array.isArray(runs)) throw new Error('Panel values require an array of runs.');
  return runs.map(run => resolvePanelData(panel, run, options));
}
