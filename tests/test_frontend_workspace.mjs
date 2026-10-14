/** Pure frontend behavior checks: panel ranges, table identity, typed Plotly
 * arrays and annotation coordinates. Browser QA covers their DOM interactions.
 * Main caller: node --test tests/test_frontend_workspace.mjs.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { tableRows, numericArray, resolvePanel, validatePanel, panelKey, columnType, prioritizeConfigKeys, isPinnedConfigKey, comparisonRows, equalValues } from '../web/workspace.mjs';
import { boxGeometry } from '../web/media-view.mjs';
import { chartSpec, renderChart } from '../web/charts.mjs';

test('config pins reorder known keys and nested children without hiding other keys', () => {
  const keys = ['batch_size', 'optimizer.beta', 'optimizer.lr', 'seed'];
  assert.deepEqual(prioritizeConfigKeys(keys, ['missing', 'seed', 'optimizer']), ['seed', 'optimizer.beta', 'optimizer.lr', 'batch_size']);
  assert.deepEqual(prioritizeConfigKeys(keys, ['unknown']), keys);
  assert.deepEqual(keys, ['batch_size', 'optimizer.beta', 'optimizer.lr', 'seed']);
  assert(isPinnedConfigKey('optimizer.lr', ['optimizer']));
  assert(!isPinnedConfigKey('optimizer_name', ['optimizer']));
  assert.deepEqual(prioritizeConfigKeys(['optimizer.lr','optimizer\\.lr'],['optimizer.lr']),['optimizer\\.lr','optimizer.lr']);
  assert(isPinnedConfigKey('optimizer\\.lr',['optimizer.lr']));
  assert(!isPinnedConfigKey('optimizer.lr',['optimizer.lr']));
});

test('structured column schemas have readable labels and preserve complete inspector details', () => {
  const schema = { wb_type: 'foreignKey', optional: false, table_id: 'examples', column: 'sample_id' };
  assert.equal(columnType(schema).label, 'Foreign key');
  assert.deepEqual(JSON.parse(columnType(schema).details), schema);
  assert.equal(columnType({ wb_type: 'primaryKey' }).label, 'Primary key');
  assert.equal(columnType({ wb_type: 'foreignIndex' }).label, 'Foreign row');
  assert.equal(columnType({ wb_type: 'classesId', class_set: [{ id: 1, name: '<script>' }] }).label, 'Class ID');
  assert.equal(columnType({ wb_type: 'futureSchema' }).label, 'futureSchema');
  assert.equal(columnType({ nested: {} }).label, 'Structured type');
  assert.deepEqual(columnType('number'), { label: 'number', details: null });
  assert.deepEqual(columnType(undefined), { label: '', details: null });
});

test('table sorting/filtering keeps source identity, handles integer columns and does not mutate rows', () => {
  const table = { columns: [0, 'score'], data: [['same', 2], ['same', 1], ['other', null], ['SAME', 1]] };
  assert.deepEqual(tableRows(table, { search: 'same', column: '0', sort: 1 }).map(row => row.index), [1,3,0]);
  assert.deepEqual(tableRows(table, { sort: 1, direction: -1 }).map(row => row.index), [0,1,3,2]);
  assert.deepEqual(table.data[0], ['same', 2]);
});

test('panel overrides inherit unset workspace settings and reject invalid axis ranges', () => {
  const defaults = { xAxis: 'defined', smoothing: .4, logScale: false };
  assert.equal(resolvePanel(defaults, { title: 'Loss' }).xAxis, 'defined');
  assert.equal(resolvePanel(defaults, { smoothing: 0 }).smoothing, 0);
  assert.notEqual(panelKey('a/b','c'), panelKey('a','b/c'));
  assert.match(validatePanel({ smoothing: 0, xMin: 10, xMax: 2 }), /minimum/);
  assert.match(validatePanel({ smoothing: 0, logScale: true, yMin: 0 }), /greater than zero/);
  assert.equal(validatePanel({ smoothing: .9, xMin: 0, xMax: 2 }), '');
});

test('manual log domains are interpreted in raw units and raw paths can be disabled', () => {
  const series = [{ name: 'Loss', color: '#b9e778', points: [{x:0,y:1,raw:2},{x:1,y:10,raw:20}] }];
  const spec = chartSpec(series, { logScale: true, yMin: .1, yMax: 100, xMin: .2, xMax: .8, smoothing: .5, showRaw: false, kind: 'area' });
  assert.equal(spec.yMin, -1); assert.equal(spec.yMax, 2); assert.equal(spec.xMin, .2);
  assert(!renderChart(spec).includes('opacity=".17"')); assert(renderChart(spec).includes('opacity=".12"'));
  assert.equal(chartSpec(series, { yMin: 100, yMax: 1 }), null);
});

test('Plotly typed arrays decode little/big endian without evaluating data', () => {
  const bytes = Buffer.alloc(16); bytes.writeDoubleLE(1.25,0); bytes.writeDoubleLE(-2.5,8);
  assert.deepEqual(numericArray({ dtype:'f8', bdata:bytes.toString('base64') }), [1.25,-2.5]);
  const big = Buffer.alloc(4); big.writeInt16BE(200,0); big.writeInt16BE(-12,2);
  assert.deepEqual(numericArray({ dtype:'>i2', bdata:big.toString('base64') }), [200,-12]);
  assert.deepEqual(numericArray({ dtype:'not-supported', bdata:'a' }), []);
});

test('image box geometry respects relative corners and pixel center formats', () => {
  assert.deepEqual(boxGeometry({ position:{minX:.1,maxX:.8,minY:.2,maxY:.7} },100,200), {x:10,y:40,width:70,height:99.99999999999999});
  assert.deepEqual(boxGeometry({ domain:'pixel',position:{middle:[30,40],width:20,height:10} },100,200), {x:20,y:35,width:20,height:10});
  assert.equal(boxGeometry({ position:{minX:3,maxX:1} },100,200), null);
});

test('comparison ignores object order while retaining array order and missing/null distinctions', () => {
  assert(equalValues([{a:1,b:{c:2}}],[{b:{c:2},a:1}]));
  assert(equalValues(-0,0));
  assert(!equalValues([1,2],[2,1]));
  assert(!equalValues(null,undefined));
  const rows = comparisonRows([new Map([['same',[{a:1,b:2}]],['different',null],['optimizer.lr',.1]]), new Map([['same',[{b:2,a:1}]],['optimizer.lr',.2]])], {differences:true,pins:['optimizer']});
  assert.deepEqual(rows.map(row=>row.key),['optimizer.lr','different']);
  assert.deepEqual(rows[1].present,[true,false]);
  assert.equal(comparisonRows([new Map([['a.b',1],['a\\.b',2]])],{search:'a\\.'})[0].key,'a\\.b');
});
