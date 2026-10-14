/**
 * Architecture: canonical run histories -> independent writer streams or one
 * grouped contribution per run -> axis metadata -> numeric chart rendering.
 * Main callers: metricPanel in app.mjs and the default chart contract tests.
 * Product rules: every history row keeps its recorded writer identity. A stream
 * without writer metadata still renders, and unattributed rows stay separate.
 * Run filtering and run limits happen in the caller before splitting, so one
 * shared run never consumes several slots in the visible-run limit.
 * Grouping counts canonical runs once. Writer-local step coordinates cannot
 * align multiple writers; shared means require a common training X coordinate.
 * Why this exists: data partitioning and axis formatting must agree across the
 * workspace, single-run charts, expanded charts, and chart exports.
 */
import {definitionFor, finite, seriesFor, valueAtKey} from './model.mjs';
import {aggregateSeries} from './run-analysis.mjs';

const axisLabels = {_step:'Step', _runtime:'Relative time (s)', _timestamp:'Wall time', defined:'Defined step'};

/** Partition a run into all of its recorded writer streams, without mutation.
 * Main callers: metricPanel, after matching and limiting the original runs.
 * Start with the canonical writers map, then add IDs observed only in history.
 * Keep unattributed rows in their own stream instead of dropping them. JSON
 * tuple keys separate the run ID, writer ID, and unattributed marker, even when
 * an ID contains punctuation. The original run object is returned for ordinary
 * histories; split streams preserve its config, definitions, and run color.
 */
export function splitRunWriters(run) {
  const history = run.history || [], writers = new Map(Object.entries(run.writers || {})), streams = new Map();
  for (const row of history) {
    const id = row._writer ?? null;
    if (!streams.has(id)) streams.set(id, []);
    streams.get(id).push(row);
    if (id !== null && !writers.has(id)) writers.set(id, {});
  }
  if (!writers.size) return [run];
  const result = [...writers].map(([id, writer]) => {
    const label = writer.label || id;
    return {...run, key:JSON.stringify([run.key, 'writer', id]), name:`${run.name} / ${label}`,
      writerId:id, writerLabel:label, history:streams.get(id) || []};
  });
  if (streams.has(null)) result.push({...run, key:JSON.stringify([run.key, 'unattributed']), name:`${run.name} / Unattributed history`,
    writerId:null, writerLabel:'Unattributed history', history:streams.get(null)});
  return result;
}

/** Build one numeric contribution for each original canonical run in a group.
 * Main callers: metricPanel, before its outer aggregateSeries call.
 * Ordinary runs use seriesFor directly. Shared runs first average their writer
 * values at identical recorded X coordinates, with one vote per writer and no
 * interpolation. The outer aggregation then gives the run one vote. Reject
 * multiple streams on writer-local Step, because equal step numbers do not
 * establish a common training coordinate. Caller supplies smoothing:0 here and
 * smooths after outer aggregation, so display smoothing is applied once.
 * Only that expected limitation has code SHARED_WRITER_LOCAL_AXIS; unrelated
 * chart errors propagate so the app can distinguish a limitation from a bug.
 */
export function groupedRunSeries(runs, metric, settings = {}) {
  return runs.map(run => {
    const streams = splitRunWriters(run), chosenAxis = settings.xAxis ?? '_step';
    const axis = chosenAxis === 'defined' ? definitionFor({...run, definitions:run.definitions || {}}, metric).step_metric || '_step' : chosenAxis;
    if (streams.length > 1 && axis === '_step') {
      const error = new Error(`Cannot group run "${run.name}": its writer-local steps cannot be averaged. Clear grouping or choose a common training X axis.`);
      error.code = 'SHARED_WRITER_LOCAL_AXIS';
      throw error;
    }
    const points = streams.length === 1 ? seriesFor(run, metric, settings) : aggregateSeries(
      streams.map(stream => ({points:seriesFor(stream, metric, settings)})), {aggregation:'mean', range:'none'});
    return {key:run.key, name:run.name, color:run.color, points};
  });
}

/** Resolve formatting metadata for the X coordinates actually being charted.
 * Main callers: metricPanel, passing its contributing original runs and metrics.
 * An explicit axis always wins. With defined axes, only metrics containing
 * numeric history contribute a declaration. A single declaration supplies its
 * label and timestamp formatting; mixed declarations remain explicitly defined
 * and expose their axes so the caller can describe that state honestly.
 * This changes labels and formatting only, never the source coordinate lookup.
 */
export function effectiveAxis(runs, metrics, xAxis = 'defined') {
  const axes = xAxis === 'defined' ? [...new Set(runs.flatMap(run => metrics.filter(metric =>
    (run.history || []).some(row => finite(valueAtKey(row, metric)))).map(metric =>
    definitionFor({...run, definitions:run.definitions || {}}, metric).step_metric || '_step')))] : [xAxis];
  const axis = xAxis === 'defined' && axes.length === 1 ? axes[0] : xAxis;
  return {axis, axes, label:axisLabels[axis] || axis, timeAxis:axis === '_timestamp', mixedAxes:axes.length > 1};
}
