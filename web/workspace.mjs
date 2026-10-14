/**
 * Architecture: local display preferences -> panel defaults and stable table
 * slices. This module has no DOM/network and never modifies logged data.
 * Main callers: app panel editor/table inspector; frontend interaction tests.
 */
import { finite, pathKey } from './model.mjs';

export const panelKey = (project, metric) => JSON.stringify([project, metric]);

/** Put recorded config pins first without hiding or inventing config keys.
 * Main callers: comparison rows and run config/overview. Pinning an object key
 * also prioritizes its flattened children; unknown pins have no visible effect.
 */
export function prioritizeConfigKeys(keys, pins = []) {
  const selected = [...new Set(pins.filter(pin => typeof pin === 'string').map(pin => pathKey([pin])))];
  const rank = key => { const index = selected.findIndex(pin => key === pin || key.startsWith(pin + '.')); return index < 0 ? Infinity : index; };
  return keys.slice().sort((left, right) => rank(left) - rank(right));
}

export const isPinnedConfigKey = (key, pins = []) => pins.some(pin => typeof pin === 'string' && (key === pathKey([pin]) || key.startsWith(pathKey([pin]) + '.')));

/** Describe both legacy type names and structured W&B table schemas.
 * Main callers: table headers and row inspector. Compact labels keep wide
 * tables readable; the complete schema stays available for inspecting linked
 * columns, required values and class labels without modifying logged data.
 */
export function columnType(schema) {
  if (schema === undefined || schema === null) return { label: '', details: null };
  if (typeof schema !== 'object') return { label: String(schema), details: null };
  const names = { primaryKey: 'Primary key', foreignKey: 'Foreign key', foreignIndex: 'Foreign row', classesId: 'Class ID' };
  const kind = typeof schema.wb_type === 'string' ? schema.wb_type : '';
  return { label: names[kind] || kind || 'Structured type', details: JSON.stringify(schema, null, 2) };
}

/** Resolve an individual panel on top of workspace defaults.
 * Main callers: metric rendering, editor preview, CSV export. Missing panel
 * options inherit live workspace defaults, rather than freezing old defaults.
 */
export function resolvePanel(workspace, local = {}) {
  return { xAxis: workspace.xAxis, smoothing: workspace.smoothing, smoothingMethod:workspace.smoothingMethod || 'twema', logScale: workspace.logScale,
    kind: workspace.kind || 'line', showRaw: workspace.showRaw ?? true, showLegend: workspace.showLegend ?? true, maxRuns: workspace.maxRuns || 10, title: '',
    ...Object.fromEntries(['xMin','xMax','yMin','yMax','xLog','xLabel','yLabel'].filter(key=>workspace[key]!==undefined).map(key=>[key,workspace[key]])), ...local };
}

/** Validate a complete editor draft before changing the saved workspace.
 * Main callers: Apply in the panel editor. Empty bounds mean automatic range.
 */
export function validatePanel(draft) {
  for (const axis of ['x', 'y']) {
    const lo = draft[`${axis}Min`], hi = draft[`${axis}Max`];
    if ((lo !== undefined && !finite(lo)) || (hi !== undefined && !finite(hi))) return `${axis.toUpperCase()} bounds must be finite numbers.`;
    if (lo !== undefined && hi !== undefined && lo >= hi) return `${axis.toUpperCase()} minimum must be less than maximum.`;
  }
  if (draft.logScale && [draft.yMin, draft.yMax].some(value => value !== undefined && value <= 0)) return 'Logarithmic Y bounds must be greater than zero.';
  if (!finite(draft.smoothing) || draft.smoothing < 0 || draft.smoothing >= 1) return 'Smoothing must be at least zero and less than one.';
  return '';
}

/** Filter/sort table rows without losing their original indices.
 * Main callers: renderTable, row inspector, filtered CSV export. Stable source
 * indices let rich-media cells and row details keep the correct identity.
 */
export function tableRows(table, { search = '', column = '', sort = null, direction = 1 } = {}) {
  const needle = search.toLocaleLowerCase();
  const index = column === '' ? -1 : Number(column);
  const rows = (table.data || []).map((cells, index) => ({ cells, index })).filter(({ cells }) => {
    const value = index < 0 ? cells : cells[index];
    return !needle || JSON.stringify(value ?? '').toLocaleLowerCase().includes(needle);
  });
  if (sort !== null) rows.sort((a, b) => {
    const x = a.cells[sort], y = b.cells[sort];
    if (x === null || x === undefined) return y === null || y === undefined ? a.index - b.index : 1;
    if (y === null || y === undefined) return -1;
    const order = finite(x) && finite(y) ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
    return order * direction || a.index - b.index;
  });
  return rows;
}

/** Compare recorded values structurally, without treating JSON key order as data.
 * Main callers: comparisonRows. Array order and missing versus null remain
 * meaningful; object insertion order does not change an experiment's value.
 */
export function equalValues(left, right) {
  if (left === right || Object.is(left, right)) return true;
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const keys = Object.keys(left), other = Object.keys(right);
  return keys.length === other.length && keys.every(key => Object.hasOwn(right, key) && equalValues(left[key], right[key]));
}

/** Build comparison rows with explicit presence and stable field identities.
 * Main callers: run comparison. Missing cells are distinct from logged nulls,
 * and filtered rows retain the same run-column order as the selected runs.
 */
export function comparisonRows(maps, { search = '', differences = false, pins = [] } = {}) {
  const keys = prioritizeConfigKeys([...new Set(maps.flatMap(map => [...map.keys()]))].sort(), pins);
  const needle = search.toLocaleLowerCase();
  return keys.filter(key => !needle || key.toLocaleLowerCase().includes(needle)).map(key => {
    const values = maps.map(map => map.get(key)), present = maps.map(map => map.has(key));
    const different = values.some((value, index) => present[index] !== present[0] || !equalValues(value, values[0]));
    return { key, values, present, different };
  }).filter(row => !differences || row.different);
}

/** Read ordinary numeric arrays or Plotly's typed-array JSON representation.
 * Main callers: Plotly renderer. No expression evaluation or remote code occurs.
 */
export function numericArray(value) {
  // Native event JSON preserves nonfinite numbers as typed cells. Decode only
  // that exact value kind so the chart's finite-point filter creates a gap,
  // while malformed coordinates remain visible validation errors.
  if (Array.isArray(value)) return value.map(item => item?._type === 'number' && ['NaN', 'Infinity', '-Infinity'].includes(item.value) ? Number(item.value) : item);
  if (!value?.bdata || typeof value.bdata !== 'string') return [];
  const types = { f8: [8, 'getFloat64'], f4: [4, 'getFloat32'], i4: [4, 'getInt32'], u4: [4, 'getUint32'], i2: [2, 'getInt16'], u2: [2, 'getUint16'], i1: [1, 'getInt8'], u1: [1, 'getUint8'] };
  const type = types[String(value.dtype).replace(/^[<>=|]/, '')];
  if (!type) return [];
  try {
    const bytes = Uint8Array.from(atob(value.bdata), char => char.charCodeAt(0));
    const view = new DataView(bytes.buffer), little = !String(value.dtype).startsWith('>');
    return Array.from({ length: Math.floor(bytes.length / type[0]) }, (_, index) => view[type[1]](index * type[0], little));
  } catch { return []; }
}
