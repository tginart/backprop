/**
 * Architecture: recorded default chart fixtures -> writer partition and axis
 * helpers -> data preservation and formatting assertions.
 * Main caller: node --test tests/test_frontend_chart_series.mjs.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeEvents, seriesFor} from '../web/model.mjs';
import {splitRunWriters, effectiveAxis, groupedRunSeries} from '../web/chart-series.mjs';
import {aggregateSeries} from '../web/run-analysis.mjs';

const run = (key, extra = {}) => ({key, name:key, config:{seed:1}, definitions:{}, history:[], color:'#123456', ...extra});

test('ordinary histories preserve the original run identity and every row', () => {
  const ordinary = run('ordinary', {history:[{_step:0, loss:4}, {_step:1, _writer:null, loss:2}]});
  assert.deepEqual(splitRunWriters(ordinary), [ordinary]);
  assert.equal(splitRunWriters(ordinary)[0], ordinary);
  assert.equal(splitRunWriters(ordinary)[0].history, ordinary.history);
});

test('shared histories keep metadata writers, unlisted writers, and unattributed rows independently', () => {
  const shared = run('shared', {writers:{primary:{id:'primary', label:'Primary'}, absent:{id:'absent'}, idle:{id:'idle', label:'Idle'}}, history:[
    {_step:0, loss:99}, {_step:1, _writer:'primary', loss:1}, {_step:1, _writer:'absent', loss:2},
    {_step:1, _writer:'history-only', loss:3}, {_step:2, _writer:null, loss:88}, {_step:2, _writer:'history-only', loss:4},
  ]});
  const before = JSON.stringify(shared), streams = splitRunWriters(shared);
  assert.equal(JSON.stringify(shared), before);
  assert.deepEqual(streams.map(item => item.writerId), ['primary', 'absent', 'idle', 'history-only', null]);
  assert.deepEqual(streams.map(item => item.name), ['shared / Primary', 'shared / absent', 'shared / Idle', 'shared / history-only', 'shared / Unattributed history']);
  assert.deepEqual(streams.map(item => item.history.map(row => row.loss)), [[1], [2], [], [3,4], [99,88]]);
  assert.equal(streams.reduce((count, item) => count + item.history.length, 0), shared.history.length);
  assert.equal(new Set(streams.flatMap(item => item.history)).size, shared.history.length);
  for (const item of streams) {assert.equal(item.color, shared.color); assert.equal(item.config, shared.config); assert.equal(item.definitions, shared.definitions);}
});

test('writer identity discovered only in history still separates colliding steps', () => {
  const shared = run('shared', {history:[{_step:0, _writer:'a', loss:1}, {_step:0, _writer:'b', loss:8}]});
  const streams = splitRunWriters(shared);
  assert.deepEqual(streams.map(item => seriesFor(item, 'loss').map(point => point.y)), [[1], [8]]);
  assert.deepEqual(streams.map(item => item.writerLabel), ['a', 'b']);
});

test('tuple keys remain distinct when run and writer IDs contain delimiters', () => {
  const first = run('a:b', {history:[{_step:0, _writer:'c', loss:1}, {_step:1, loss:2}]}), second = run('a', {history:[{_step:0, _writer:'b:c', loss:3}, {_step:1, _writer:'unattributed', loss:4}]});
  const keys = [...splitRunWriters(first), ...splitRunWriters(second)].map(item => item.key);
  assert.equal(new Set(keys).size, 4);
  assert.deepEqual(JSON.parse(keys[0]), ['a:b', 'writer', 'c']);
  assert.deepEqual(JSON.parse(keys[1]), ['a:b', 'unattributed']);
});

test('run limiting stays with the caller and does not truncate writers in a selected run', () => {
  const runs = Array.from({length:12}, (_, index) => run(`run-${index}`, {history:Array.from({length:15}, (_, writer) => ({_step:0, _writer:`writer-${writer}`, loss:writer}))}));
  assert.equal(splitRunWriters(runs[0]).length, 15);
  const streams = runs.slice(0, 10).flatMap(splitRunWriters);
  assert.equal(streams.length, 150);
  assert.deepEqual(new Set(streams.map(item => JSON.parse(item.key)[0])), new Set(runs.slice(0, 10).map(item => item.key)));
});

test('a default declared timestamp axis uses wall-time formatting', () => {
  const events = [
    {event_id:'definition', entity:'team', project:'project', run_id:'a', type:'metric_definition', timestamp:'2026-10-07T00:00:00Z', payload:{name:'loss', step_metric:'_timestamp'}},
    {event_id:'history', entity:'team', project:'project', run_id:'a', type:'history', timestamp:'2026-10-07T00:01:00Z', payload:{step:0, metrics:{loss:2}}},
  ];
  const {runs} = normalizeEvents(events), original = seriesFor(runs[0], 'loss', {xAxis:'defined'});
  assert.deepEqual(effectiveAxis(runs, ['loss']), {axis:'_timestamp', axes:['_timestamp'], label:'Wall time', timeAxis:true, mixedAxes:false});
  assert.deepEqual(seriesFor(runs[0], 'loss', {xAxis:'defined'}), original);
  assert.equal(effectiveAxis(runs, ['loss'], '_step').timeAxis, false);
  assert.equal(effectiveAxis(runs, ['loss'], '_timestamp').timeAxis, true);
});

test('unified custom and wildcard definitions resolve exactly, while mixed axes remain explicit', () => {
  const first = run('a', {definitions:{'train/*':{step_metric:'tokens'}}, history:[{_step:0, tokens:100, 'train/loss':4}]}), second = run('b', {history:[{_step:0, 'train/loss':2}]});
  assert.deepEqual(effectiveAxis([first], ['train/loss']), {axis:'tokens', axes:['tokens'], label:'tokens', timeAxis:false, mixedAxes:false});
  assert.deepEqual(effectiveAxis([first, second], ['train/loss']), {axis:'defined', axes:['tokens','_step'], label:'Defined step', timeAxis:false, mixedAxes:true});
  assert.deepEqual(effectiveAxis([first, second], ['train/loss'], '_runtime'), {axis:'_runtime', axes:['_runtime'], label:'Relative time (s)', timeAxis:false, mixedAxes:false});
});

test('a metric absent from a run contributes no misleading axis declaration', () => {
  const first = run('a', {definitions:{loss:{step_metric:'epoch'}}, history:[{_step:0, epoch:1, loss:4}]}), unrelated = run('b', {definitions:{loss:{step_metric:'_timestamp'}}, history:[{_step:0, other:7}]});
  assert.equal(effectiveAxis([first, unrelated], ['loss']).label, 'epoch');
  assert.deepEqual(effectiveAxis([], ['loss']), {axis:'defined', axes:[], label:'Defined step', timeAxis:false, mixedAxes:false});
});

test('shared writer means contribute one vote per run to the outer group', () => {
  const shared = run('shared', {definitions:{loss:{step_metric:'epoch'}}, history:[{_step:0, _writer:'a', epoch:1, loss:1}, {_step:50, _writer:'b', epoch:1, loss:3}]}),
    ordinary = run('ordinary', {definitions:{loss:{step_metric:'epoch'}}, history:[{_step:9, epoch:1, loss:8}]});
  const runs = [shared, ordinary], before = JSON.stringify(runs), lines = groupedRunSeries(runs, 'loss', {xAxis:'defined', smoothing:0});
  assert.deepEqual(lines.map(line => [line.key, line.points.map(point => [point.x, point.y])]), [['shared', [[1,2]]], ['ordinary', [[1,8]]]]);
  const outer = aggregateSeries(lines, {aggregation:'mean', range:'none'});
  assert.equal(outer[0].y, 5); assert.equal(outer[0].count, 2);
  assert.equal(JSON.stringify(runs), before);
  assert.equal(lines[0].name, shared.name); assert.equal(lines[0].color, shared.color);
  assert.deepEqual(lines[1].points, seriesFor(ordinary, 'loss', {xAxis:'defined', smoothing:0}));
});

test('grouped shared values use recorded coordinates only, without filling missing contributions', () => {
  const shared = run('shared', {history:[
    {_step:0, _writer:'a', epoch:0, loss:2}, {_step:1, _writer:'a', epoch:2, loss:4},
    {_step:0, _writer:'b', epoch:1, loss:10}, {_step:1, _writer:'b', epoch:2, loss:6},
    {_step:2, _writer:'b', epoch:3},
  ]}), ordinary = run('ordinary', {history:[{_step:0, epoch:0, loss:8}, {_step:1, epoch:2, loss:9}]});
  const lines = groupedRunSeries([shared, ordinary], 'loss', {xAxis:'epoch', smoothing:0});
  assert.deepEqual(lines[0].points.map(point => [point.x, point.y, point.count]), [[0,2,1], [1,10,1], [2,5,2]]);
  assert.deepEqual(aggregateSeries(lines, {aggregation:'mean', range:'none'}).map(point => [point.x, point.y, point.count]), [[0,5,2], [1,10,1], [2,7,2]]);
});

test('each writer has one vote at a repeated training coordinate', () => {
  const shared = run('shared', {history:[{_step:0, _writer:'a', epoch:1, loss:1}, {_step:1, _writer:'a', epoch:1, loss:10}, {_step:0, _writer:'b', epoch:1, loss:0}]});
  const [line] = groupedRunSeries([shared], 'loss', {xAxis:'epoch'});
  assert.deepEqual(line.points.map(point => [point.x, point.y, point.count]), [[1,5,2]]);
});

test('grouping multiwriter local steps rejects explicitly, including undefined step definitions', () => {
  const shared = run('shared', {history:[{_step:0, _writer:'a', loss:1}, {_step:0, _writer:'b', loss:3}]});
  for (const settings of [{}, {xAxis:'_step'}, {xAxis:'defined'}]) assert.throws(() => groupedRunSeries([shared], 'loss', settings), error => {
    assert.equal(error.code, 'SHARED_WRITER_LOCAL_AXIS');
    assert.match(error.message, /Cannot group run "shared".*writer-local steps cannot be averaged.*Clear grouping/);
    return true;
  });
  const ordinary = run('ordinary', {history:[{_step:0, loss:8}]});
  assert.deepEqual(groupedRunSeries([ordinary], 'loss')[0].points, seriesFor(ordinary, 'loss'));
  const oneWriter = run('one-writer', {history:[{_step:0, _writer:'a', loss:4}]});
  assert.equal(groupedRunSeries([oneWriter], 'loss')[0].points[0].y, 4);
});

test('unrelated chart errors keep their own identity instead of becoming grouping limitations', () => {
  const ordinary = run('ordinary', {history:[{_step:0, loss:8}]});
  assert.throws(() => groupedRunSeries([ordinary], 'loss', {smoothing:-1}), error => {
    assert.equal(error.code, undefined); assert.match(error.message, /Smoothing must/); return true;
  });
});
