/**
 * Architecture: recorded runs -> typed fields -> validated filters and stable
 * sorting -> run groups -> coordinate-aligned aggregate curves. No DOM, storage,
 * network or mutation of logged data belongs here.
 * Main callers: app run table/sidebar, filter editor, chart previews and exports.
 * Why this exists: the sidebar, full table and plots must agree about which runs
 * match and how missing data differs from a recorded zero or null.
 */
import { flatten, valueAtKey, finite, isRich } from './model.mjs';
import { equalValues } from './workspace.mjs';

export const RUN_FIELDS = [
  ['run:name', 'Name'], ['run:id', 'ID'], ['run:state', 'State'],
  ['run:group', 'Group'], ['run:tags', 'Tags'], ['run:created_at', 'Created'],
  ['run:updated_at','Updated'],['run:finished_at','End time'],['run:job_type','Job type'],['run:notes','Notes'],['run:description','Description'],['run:runtime','Runtime (s)'],
];
export const FILTER_OPERATORS = [
  ['eq', '='], ['ne', '≠'], ['lt', '<'], ['lte', '≤'], ['gt', '>'], ['gte', '≥'],
  ['contains', 'contains'], ['not-contains', 'does not contain'],
  ['exists', 'is present'], ['missing', 'is missing'],
];

/** Resolve one qualified field, preserving literal dots via model path escaping.
 * Main callers: filter matching, table cells, sort/group keys.
 */
export function runField(run, field) {
  const separator = field.indexOf(':');
  const source = field.slice(0, separator), key = field.slice(separator + 1);
  if(field==='run:runtime'){const durations=(run.history || []).map(row=>row._runtime).filter(finite);return durations.length?Math.max(...durations):undefined;}
  if (source === 'run' && RUN_FIELDS.some(([name]) => name === field)) return run[key];
  if (source === 'config' || source === 'summary') return valueAtKey(run[source], key);
  throw new Error(`Unknown run field: ${field}`);
}

export function analysisFields(runs) {
  const fields = new Map(RUN_FIELDS);
  for (const source of ['config', 'summary']) {
    const keys = new Set(runs.flatMap(run => flatten(run[source] || {}).filter(([, value]) => !isRich(value) || value?._type === 'number').map(([key]) => key)));
    for (const key of [...keys].sort()) fields.set(`${source}:${key}`, `${source === 'config' ? 'Config' : 'Summary'} · ${key}`);
  }
  return [...fields];
}

/** Parse the filter operand once, before accepting the user's draft.
 * Main callers: validateFilters/matchesFilters. Numbers, booleans and null use
 * JSON spelling; unquoted input means a string. Numeric operators never coerce
 * a recorded string into a number. Invalid numeric input is an explicit error.
 */
export function filterOperand(filter) {
  if (filter.op === 'exists' || filter.op === 'missing') return undefined;
  const text = String(filter.value ?? '').trim();
  if (['lt', 'lte', 'gt', 'gte'].includes(filter.op)) {
    if (!text || !finite(Number(text))) throw new Error('Numeric comparisons require a finite number.');
    return Number(text);
  }
  if (['contains', 'not-contains'].includes(filter.op)) return String(filter.value ?? '');
  if (/^-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text)) {
    if (!finite(Number(text))) throw new Error('Numeric filters require a finite number.');
    return Number(text);
  }
  if (/^(true|false|null)$/.test(text) || text.startsWith('"') || text.startsWith('[') || text.startsWith('{')) {
    try { return JSON.parse(text); } catch { throw new Error('Use valid JSON for quoted strings, arrays or objects.'); }
  }
  return String(filter.value ?? '');
}

export function validateFilters(filters) {
  if (!Array.isArray(filters)) throw new Error('Run filters must be a list.');
  for (const filter of filters) {
    if (!filter || typeof filter.field !== 'string' || !/^(run|config|summary):.+/.test(filter.field)) throw new Error('Choose a run, configuration or summary field.');
    // Validate known run properties even when the current project has no runs.
    runField({}, filter.field);
    if (!FILTER_OPERATORS.some(([op]) => op === filter.op)) throw new Error('Choose a supported comparison.');
    filterOperand(filter);
  }
  return filters;
}

function comparable(value) {
  return value?._type === 'number' ? Number(value.value) : value;
}

/** Match every filter (AND) without treating missing as a value.
 * Main callers: selectAnalysisRuns and the filter editor's match count.
 * Equality is structural and type-aware; array containment tests elements.
 */
export function matchesFilters(run, filters) {
  return filters.every(filter => {
    const recorded = runField(run, filter.field), present = recorded !== undefined;
    if (filter.op === 'exists') return present;
    if (filter.op === 'missing') return !present;
    if (!present) return false;
    const value = comparable(recorded), expected = filterOperand(filter);
    switch (filter.op) {
      case 'eq': return equalValues(value, expected);
      case 'ne': return !equalValues(value, expected);
      case 'lt': return finite(value) && value < expected;
      case 'lte': return finite(value) && value <= expected;
      case 'gt': return finite(value) && value > expected;
      case 'gte': return finite(value) && value >= expected;
      case 'contains': case 'not-contains': {
        const contains = Array.isArray(value) ? value.some(item => equalValues(item, expected))
          : typeof value === 'string' && value.toLocaleLowerCase().includes(expected.toLocaleLowerCase());
        return filter.op === 'contains' ? contains : !contains;
      }
      default: throw new Error(`Unknown filter operator: ${filter.op}`);
    }
  });
}

/** Apply the same selection pipeline to the sidebar, table and plotted runs.
 * Main callers: app.filteredRuns. Missing/nonfinite sort values always go last,
 * including descending order. Original order breaks ties deterministically.
 */
export function selectAnalysisRuns(runs, {search = '', searchRegex = false, status = '', group = '', filters = [], sort = 'run:created_at', direction = -1} = {}) {
  const needle = search.toLocaleLowerCase(), pattern = searchRegex ? new RegExp(search, 'i') : null;
  return runs.map((run, index) => ({run, index})).filter(({run}) =>
    (!status || run.state === status) && (!group || run.group === group) &&
    (!needle || (pattern ? pattern.test([run.name, run.id, run.group, ...(run.tags || [])].join(' ')) : [run.name, run.id, run.group, ...(run.tags || [])].join(' ').toLocaleLowerCase().includes(needle))) && matchesFilters(run, filters)
  ).sort((a, b) => {
    let x = comparable(runField(a.run, sort)), y = comparable(runField(b.run, sort));
    if (['run:created_at','run:updated_at','run:finished_at'].includes(sort)) { x = x ? Date.parse(x) : undefined; y = y ? Date.parse(y) : undefined; }
    const missing = value => value === undefined || value === null || typeof value === 'number' && !finite(value);
    if (missing(x) || missing(y)) return missing(x) ? missing(y) ? a.index - b.index : 1 : -1;
    const comparison = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), undefined, {numeric:true});
    return comparison * direction || a.index - b.index;
  }).map(({run}) => run);
}

function identity(value) {
  if (value === undefined) return ['missing'];
  if (Array.isArray(value)) return ['array', value.map(identity)];
  if (value !== null && typeof value === 'object') return ['object', Object.keys(value).sort().map(key => [key, identity(value[key])])];
  return [typeof value, value];
}
export function groupRuns(runs, field) {
  if (!field) return runs.map(run => ({key:run.key, name:run.name, runs:[run]}));
  const groups = new Map();
  for (const run of runs) {
    const value = field === 'all' ? 'All selected runs' : runField(run, field);
    const key = JSON.stringify([field, identity(value)]);
    if (!groups.has(key)) groups.set(key, {key, name: value === undefined ? 'Not logged' : value === null ? 'null' : typeof value === 'object' ? JSON.stringify(value) : String(value), runs:[]});
    groups.get(key).runs.push(run);
  }
  return [...groups.values()];
}

/** Aggregate one value per run at each recorded X coordinate.
 * Main callers: metricPanel. Uneven histories contribute only where they have
 * an actual point: no extrapolation, fabricated interpolation or repeated vote
 * from one run. The last recorded point wins for a duplicate X within a run.
 * Standard deviation is population deviation; standard error divides by sqrt(n).
 * These explicit alignment rules do not claim W&B's opaque server bucket layout.
 */
export function aggregateSeries(series, {aggregation = 'mean', range = 'stddev'} = {}) {
  if (!['mean', 'median', 'min', 'max'].includes(aggregation)) throw new Error('Unknown group aggregation.');
  if (!['none', 'minmax', 'stddev', 'stderr'].includes(range)) throw new Error('Unknown group range.');
  const coordinates = new Map();
  for (const line of series) {
    const unique = new Map();
    for (const point of line.points) if (finite(point.x) && finite(point.y)) unique.set(point.x, point);
    for (const [x, point] of unique) {
      if (!coordinates.has(x)) coordinates.set(x, []);
      coordinates.get(x).push(point);
    }
  }
  return [...coordinates].sort(([a], [b]) => a - b).map(([x, points]) => {
    const values = points.map(point => point.y).sort((a, b) => a - b), count = values.length;
    const mean = values.reduce((sum, value) => sum + value / count, 0);
    const stddev = Math.sqrt(values.reduce((sum, value) => sum + ((value - mean) ** 2) / count, 0));
    const min = values[0], max = values.at(-1), median = count % 2 ? values[(count - 1) / 2] : values[count / 2 - 1] / 2 + values[count / 2] / 2;
    const y = {mean, median, min, max}[aggregation];
    const amount = range === 'stderr' ? stddev / Math.sqrt(count) : stddev;
    const band = range === 'none' ? null : range === 'minmax' ? [min, max] : [mean - amount, mean + amount];
    return {x, y, raw:y, ...(points.some(point=>point.breakBefore)?{breakBefore:true}:{}), count, mean, stddev, min, max, ...(band ? {band} : {})};
  });
}
