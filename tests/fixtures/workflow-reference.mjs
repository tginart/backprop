/**
 * Architecture: fixed synthetic experiment data -> SDK-shaped event envelopes
 * -> Foundry-shaped records and media -> the real browser application.
 *
 * Main callers: tests/ui_workflow_reference_qa.mjs and focused fixture checks.
 * Why this exists: four demo runs cannot expose pagination, long labels, many
 * sections, missing metrics, or the difference between row selection and plot
 * visibility. This fixture describes data, never inferred W&B behavior.
 *
 * Invariants: every timestamp and value is deterministic; no user data, network
 * calls, Python runtime, downloaded assets, or installed packages are required.
 * Scalar gaps are deliberate. Missing, null, and recorded zero stay distinct.
 */
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {deflateSync} from 'node:zlib';

export const WORKFLOW_RUN_COUNT = 256;
export const WORKFLOW_HISTORY_STEPS = 12;
export const WORKFLOW_PROJECT = 'workflow-reference-fixture';
export const WORKFLOW_ENTITY = 'demo-user';
export const WORKFLOW_BASE_TIME = '2026-10-07T12:00:00.000Z';
export const WORKFLOW_SECTIONS = [
  'train', 'validation', 'evaluation', 'optimizer', 'model', 'diagnostics',
  'system', 'transfer_learning_for_long_named_multilingual_evaluation',
];
const metricLabels = [
  'loss', 'accuracy', 'precision', 'recall', 'f1', 'gradient_norm',
  'learning_rate', 'throughput', 'memory_used', 'latency', 'score',
  'confidence_calibration_error_for_out_of_distribution_samples_with_missing_labels',
];
export const WORKFLOW_METRICS = WORKFLOW_SECTIONS.flatMap(section => metricLabels.map(name => `${section}/${name}`));
export const WORKFLOW_RECORD_HEADERS = [
  'schema_version', 'event_id', 'run_id', 'project', 'entity', 'event_type',
  'timestamp', 'payload',
];
const groupNames = [
  'baseline', 'candidate-a', 'candidate-b', 'ablation', 'transfer-learning',
  'long-context', 'missing-metrics', 'repeated-seeds',
];
const optimizerNames = ['adam', 'adamw', 'sgd', 'lion'];
const padded = number => String(number).padStart(4, '0');
const digest = body => createHash('sha256').update(body).digest('hex');
const rounded = number => Number(number.toFixed(6));

/** Create local PNG bytes with two visible classes using Node's built-in zlib.
 * Main callers: createWorkflowReferenceFixture. The mask uses grayscale class
 * IDs, while the image contains a matching rectangle and colored background.
 */
function imageBytes(variant, mask = false) {
  const width = 320, height = 192;
  const pixels = Buffer.alloc(height * (width * 3 + 1));
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const foreground = x > 56 + variant * 3 && x < 244 && y > 24 && y < 160;
      const colors = mask ? [foreground ? 1 : 0, foreground ? 1 : 0, foreground ? 1 : 0]
        : foreground ? [66 + variant * 22, 166, 136 + Math.floor(x / 12)]
          : [16 + Math.floor(y / 14), 27 + Math.floor(x / 25), 38 + variant * 11];
      const offset = y * (width * 3 + 1) + x * 3 + 1;
      colors.forEach((value, channel) => { pixels[offset + channel] = value; });
    }
  }
  const chunk = (kind, data) => {
    const content = Buffer.concat([Buffer.from(kind), data]);
    let checksum = 0xffffffff;
    for (const byte of content) {
      checksum ^= byte;
      for (let bit = 0; bit < 8; bit++) checksum = (checksum >>> 1) ^ ((checksum & 1) ? 0xedb88320 : 0);
    }
    const bytes = Buffer.alloc(data.length + 12);
    bytes.writeUInt32BE(data.length);
    content.copy(bytes, 4);
    bytes.writeUInt32BE((checksum ^ 0xffffffff) >>> 0, data.length + 8);
    return bytes;
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width); header.writeUInt32BE(height, 4);
  header[8] = 8; header[9] = 2;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', header), chunk('IDAT', deflateSync(pixels)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

/** Compute a predictable curve without making missing values into zero.
 * Main callers: scalarHistory. Metric kind selects the scale and shape, while
 * run index creates variation for grouping, sorting, hover, and legend tests.
 */
function scalarValue(runIndex, metricIndex, step) {
  const kind = metricIndex % metricLabels.length;
  const progress = step / (WORKFLOW_HISTORY_STEPS - 1);
  const offset = (runIndex % 32) / 500 + Math.floor(metricIndex / 12) / 100;
  if (kind === 0) return rounded(1.4 * Math.exp(-progress * 3) + .06 + offset);
  if ([1, 2, 3, 4, 10].includes(kind)) return rounded(.48 + .46 * progress + offset / 3);
  if (kind === 5) return rounded(.7 * Math.exp(-progress * 2) + .05 + offset);
  if (kind === 6) return rounded(.003 * (1 - progress * .8) + (runIndex % 4) * .0001);
  if (kind === 7) return rounded(900 + (runIndex % 16) * 23 + step * 11);
  if (kind === 8) return rounded(2048 + (runIndex % 8) * 128 + step * 7);
  if (kind === 9) return rounded(8 + (runIndex % 12) / 2 + Math.sin(step / 2));
  return rounded(.21 * (1 - progress) + offset / 2);
}

/** Leave whole-series and middle-point gaps in selected runs.
 * Main callers: createWorkflowReferenceFixture. Run zero always contains every
 * scalar name so a single-run chart bank retains all eight test sections.
 */
function scalarHistory(runIndex, step) {
  const metrics = {epoch: step};
  WORKFLOW_METRICS.forEach((name, metricIndex) => {
    if (runIndex > 0 && (runIndex + metricIndex) % 19 === 0) return;
    if (runIndex > 0 && step > 0 && step < WORKFLOW_HISTORY_STEPS - 1 && (runIndex + metricIndex + step) % 17 === 0) return;
    if (runIndex > 0 && step === 6 && (runIndex + metricIndex) % 13 === 0) {
      metrics[name] = null;
      return;
    }
    metrics[name] = metricIndex === 95 && runIndex === 0 && step === 0
      ? 0 : scalarValue(runIndex, metricIndex, step);
  });
  return metrics;
}

/** Build hundreds of runs plus rich outputs in the ordinary event format.
 * Main callers: the dedicated browser runner and assertWorkflowReferenceFixture.
 * Stage boundary: this creates envelopes; the app's real model remains
 * responsible for replay, summaries, metric discovery, and media rendering.
 */
export function createWorkflowReferenceFixture({
  entity = WORKFLOW_ENTITY,
  project = WORKFLOW_PROJECT,
  mediaDatasetRid = 'ri.foundry.main.dataset.workflow-reference-media',
} = {}) {
  const events = [], files = {}, runNames = {}, expectedStates = {};
  let sequence = 0;
  const base = Date.parse(WORKFLOW_BASE_TIME);
  const add = (runIndex, type, payload, seconds) => {
    events.push({
      schema_version: 1, event_id: `workflow-reference-${padded(sequence++)}`,
      timestamp: new Date(base + runIndex * 60000 + seconds * 1000).toISOString(),
      run_id: `workflow-${padded(runIndex)}`, entity, project, type, payload,
    });
  };
  const addFile = (path, body, contentType) => {
    files[path] = {body, contentType};
    return {dataset_rid: mediaDatasetRid, path, mime_type: contentType, size: body.length};
  };
  const images = Array.from({length: 4}, (_, index) => addFile(`blobs/workflow/image-${index}.png`, imageBytes(index), 'image/png'));
  const mask = addFile('blobs/workflow/mask.png', imageBytes(0, true), 'image/png');

  for (let runIndex = 0; runIndex < WORKFLOW_RUN_COUNT; runIndex++) {
    const runId = `workflow-${padded(runIndex)}`;
    const group = groupNames[runIndex % groupNames.length];
    const name = `experiment-${padded(runIndex)}-${optimizerNames[runIndex % 4]}`
      + (runIndex % 11 === 0 ? '-multilingual-long-context-transfer-learning-with-repeated-seeds-and-out-of-distribution-validation' : '');
    runNames[runId] = name;
    const config = {
      optimizer: optimizerNames[runIndex % 4], learning_rate: [.003, .001, .01, .0005][runIndex % 4],
      batch_size: [16, 32, 64, 128][runIndex % 4], seed: runIndex % 32,
      dataset: {name: 'synthetic-classification', split: runIndex % 2 ? 'candidate' : 'baseline'},
      architecture: {encoder: 'fixture-transformer', layers: 6 + runIndex % 6},
      demonstration: true,
      ...(runIndex % 9 ? {accelerator_count: 1 + runIndex % 4} : {}),
      ...(runIndex % 7 === 0 ? {optional_evaluation: null} : {}),
      ...(runIndex % 13 === 0 ? {long_configuration_name_for_shared_sidebar_and_full_table_scope_checks: `cohort-${runIndex % 8}`} : {}),
    };
    add(runIndex, 'run_start', {
      name, group, job_type: runIndex % 3 ? 'train' : 'evaluate', config,
      tags: ['synthetic-demo', 'reference-workflow', runIndex % 2 ? 'candidate' : 'baseline', `cohort-${runIndex % 8}`],
      notes: `Synthetic reference workflow run ${padded(runIndex)}. No user experiments are included.`,
    }, 0);
    add(runIndex, 'metric_definition', {name: '*', step_metric: 'epoch'}, 1);
    add(runIndex, 'metric_definition', {name: 'epoch', hidden: true}, 2);
    for (let step = 0; step < WORKFLOW_HISTORY_STEPS; step++) {
      add(runIndex, 'history', {step, metrics: scalarHistory(runIndex, step)}, 3 + step);
    }
    add(runIndex, 'summary', {values: {
      final_accuracy: scalarValue(runIndex, 1, 11), final_loss: scalarValue(runIndex, 0, 11),
      ...(runIndex % 5 ? {evaluation_samples: 1024 + runIndex * 8} : {}),
      ...(runIndex % 7 === 0 ? {optional_score: null} : {}),
      ...(runIndex % 10 === 0 ? {recorded_zero: 0} : {}),
    }}, 16);
    add(runIndex, 'run_metadata', {values: {pinned_config_keys: ['learning_rate', 'optimizer']}}, 17);

    if (runIndex < 8) {
      const table = {
        _type: 'table', table_id: `${runId}-evaluation`, log_mode: 'IMMUTABLE',
        columns: ['sample_id', 'prompt', 'prediction', 'actual', 'confidence', 'preview'],
        column_types: ['string', 'string', 'string', 'string', 'number', {wb_type: 'image-file'}],
        data: Array.from({length: 96}, (_, index) => [
          `sample-${padded(index)}`,
          index % 7 === 0 ? `Long synthetic evaluation prompt ${index}.\n` + 'Repeated context makes full row inspection necessary. '.repeat(24) : `Synthetic evaluation prompt ${index}`,
          index % 5 ? 'rectangle' : 'circle', 'rectangle', index % 11 === 0 ? null : rounded(.55 + (index % 43) / 100),
          index % 8 === 0 ? {_type: 'image', ...images[(runIndex + index) % 4], caption: `Synthetic sample ${index}`} : null,
        ]),
      };
      const tableFile = addFile(`blobs/workflow/${runId}/evaluation.table.json`, Buffer.from(JSON.stringify(table)), 'application/json');
      const logFile = addFile(`blobs/workflow/${runId}/training.txt`, Buffer.from(`Synthetic reference training log\nRun: ${runId}\nHistory steps: ${WORKFLOW_HISTORY_STEPS}\n`), 'text/plain');
      const artifactId = `workflow-evaluation-${padded(runIndex)}`;
      add(runIndex, 'history', {step: WORKFLOW_HISTORY_STEPS, metrics: {
        'samples/predictions': table,
        'samples/image': {
          _type: 'image', ...images[runIndex % 4], caption: 'Synthetic segmentation fixture',
          boxes: {prediction: {box_data: [{position: {minX: .175, maxX: .763, minY: .125, maxY: .833}, class_id: 1, box_caption: 'rectangle', scores: {confidence: .94}}], class_labels: {'1': 'rectangle'}}},
          masks: {segmentation: {mask: {_type: 'image', ...mask}, class_labels: {'0': 'background', '1': 'rectangle'}}},
        },
        'samples/gallery': images.slice(0, 2).map((file, index) => ({_type: 'image', ...file, caption: `Synthetic gallery image ${index + 1}`})),
        'evaluation/confidence_distribution': {_type: 'histogram', values: [1, 3, 6, 10, 18, 25, 30, 22], bins: [0, .125, .25, .375, .5, .625, .75, .875, 1]},
        'evaluation/class_accuracy': {_type: 'plot', kind: 'bar', table: {columns: ['class', 'accuracy'], data: [['rectangle', .94], ['circle', .87], ['triangle', .78]]}, fields: {x: 'class', y: 'accuracy'}, title: 'Synthetic class accuracy'},
      }}, 18);
      add(runIndex, 'console', {text: `Synthetic training session ${padded(runIndex)}\nEpoch 11/11: loss=${scalarValue(runIndex, 0, 11)}\n`, stream: 'stdout'}, 19);
      add(runIndex, 'console', {text: 'Evaluation includes deliberately missing confidence values.\n', stream: 'stderr'}, 20);
      add(runIndex, 'file', {name: 'training.txt', policy: 'end', file: logFile}, 21);
      add(runIndex, 'artifact', {
        artifact_id: artifactId, name: 'workflow-evaluation', artifact_type: 'dataset',
        version: `v${runIndex}`, state: 'COMMITTED', aliases: [`cohort-${runIndex}`],
        description: 'Synthetic immutable evaluation results for workflow inspection.',
        digest: digest(files[tableFile.path].body), metadata: {rows: 96, synthetic: true},
        files: [{name: 'evaluation.table.json', ...tableFile, digest: digest(files[tableFile.path].body)}],
      }, 22);
      add(runIndex, 'artifact_use', {artifact_id: artifactId, name: 'workflow-evaluation', artifact_type: 'dataset', version: `v${runIndex}`, source_entity: entity, source_project: project}, 23);
    }
    const state = runIndex % 16 === 0 ? 'running' : runIndex % 8 === 1 ? 'failed' : 'finished';
    expectedStates[runId] = state;
    if (state !== 'running') add(runIndex, 'run_finish', {state, exit_code: state === 'failed' ? 1 : 0}, 24);
  }

  const fixture = {events, files, manifest: {
    entity, project, projectId: `${entity}/${project}`, mediaDatasetRid,
    runCount: WORKFLOW_RUN_COUNT, scalarMetricNames: [...WORKFLOW_METRICS],
    sectionNames: [...WORKFLOW_SECTIONS], historySteps: WORKFLOW_HISTORY_STEPS,
    groupNames: [...groupNames], runNames, expectedStates,
    primaryRunId: 'workflow-0000', primaryRunKey: `${entity}/${project}/workflow-0000`,
    richRunIds: Array.from({length: 8}, (_, index) => `workflow-${padded(index)}`),
    longNameRunId: 'workflow-0000', longMetric: WORKFLOW_METRICS.at(-1),
    absentConfigRunId: 'workflow-0000', absentConfigField: 'config:accelerator_count',
    presentConfigRunId: 'workflow-0001', presentConfigField: 'config:accelerator_count',
    deliberateMissingValues: true, privateReferenceData: false,
  }};
  assertWorkflowReferenceFixture(fixture);
  return fixture;
}

/** Validate data coverage before a browser run can claim meaningful evidence.
 * Main callers: fixture construction and the dedicated runner. These checks
 * establish test-data coverage only; they cannot establish reference parity.
 */
export function assertWorkflowReferenceFixture({events, files, manifest}) {
  const starts = events.filter(event => event.type === 'run_start');
  assert.equal(starts.length, WORKFLOW_RUN_COUNT);
  assert.equal(new Set(starts.map(event => event.run_id)).size, WORKFLOW_RUN_COUNT);
  assert.equal(new Set(events.map(event => event.event_id)).size, events.length);
  assert.equal(manifest.scalarMetricNames.length, 96);
  assert.equal(new Set(manifest.sectionNames).size, 8);
  assert.equal(new Set(starts.map(event => event.payload.group)).size, 8);
  assert(starts.some(event => event.payload.name.length > 100));
  assert(manifest.longMetric.length > 100);
  assert(starts.some(event => !Object.hasOwn(event.payload.config, 'accelerator_count')));
  assert(starts.some(event => event.payload.config.optional_evaluation === null));
  const history = events.filter(event => event.type === 'history');
  const scalarNames = new Set(history.flatMap(event => Object.entries(event.payload.metrics).filter(([, value]) => typeof value === 'number').map(([name]) => name)));
  assert(manifest.scalarMetricNames.every(name => scalarNames.has(name)));
  assert(history.some(event => Object.values(event.payload.metrics).includes(null)));
  assert(history.some(event => typeof event.payload.metrics[WORKFLOW_METRICS[0]] === 'number'));
  assert(history.some(event => !Object.hasOwn(event.payload.metrics, WORKFLOW_METRICS[0])));
  const rich = history.flatMap(event => Object.values(event.payload.metrics));
  for (const type of ['table', 'image', 'histogram', 'plot']) assert(rich.some(value => value?._type === type));
  assert(Object.values(files).every(file => Buffer.isBuffer(file.body) && typeof file.contentType === 'string'));
  assert.equal(Object.values(manifest.expectedStates).filter(state => state === 'running').length, 16);
  assert.equal(Object.values(manifest.expectedStates).filter(state => state === 'failed').length, 32);
  return true;
}

/** Encode envelopes as the same records and CSV returned by Foundry APIs.
 * Main callers: the dedicated runner's stream and archive endpoint intercepts.
 */
export function workflowFoundryRecords(events) {
  return events.map(({type, payload, ...rest}) => ({...rest, event_type: type, payload: JSON.stringify(payload)}));
}
export function workflowFoundryCsv(records) {
  const rows = [WORKFLOW_RECORD_HEADERS, ...records.map(record => WORKFLOW_RECORD_HEADERS.map(key => record[key]))];
  return rows.map(row => row.map(value => `"${String(value ?? '').replaceAll('"', '""')}"`).join(',')).join('\n');
}
