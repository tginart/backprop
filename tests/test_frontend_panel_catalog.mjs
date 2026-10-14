/**
 * Architecture: canonical logged-event fixtures -> panel catalog/definitions ->
 * snapshot assertions. Main caller: node --test tests/test_frontend_panel_catalog.mjs.
 * These checks cover data identity and state boundaries; browser QA owns layout.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeEvents} from '../web/model.mjs';
import {PANEL_TYPES, panelDefaults, discoverPanelSources, createPanel, materializePanels, panelVersions, resolvePanelData, resolvePanelValues} from '../web/panel-catalog.mjs';

const rich = type => ({_type:type, ...(type === 'table' ? {columns:['value'], data:[[1]]} : {caption:type})});
const event = (id, step, metrics, extra = {}) => ({event_id:id, type:'history', entity:'team', project:'project', run_id:extra.run_id || 'a',
  timestamp:`2026-01-01T00:00:${String(step).padStart(2, '0')}Z`, payload:{step, metrics}, ...extra});
const replay = events => normalizeEvents(events).runs;

test('discovery supports the existing scalar, rich, media-list and text renderers', () => {
  const metrics = {loss:1, table:rich('table'), image:rich('image'), audio:rich('audio'), video:rich('video'), histogram:rich('histogram'),
    plot:rich('plot'), plotly:rich('plotly'), vega:rich('vega'), html:rich('html'), graph:rich('graph-file'), media:[rich('image')], text:'hello', json:{_type:'ndarray', data:[1,2]}};
  const runs = replay([event('a', 0, metrics)]), sources = discoverPanelSources(runs);
  assert.deepEqual(sources.map(source => source.type).sort(), PANEL_TYPES.filter(type=>type!=='run-comparer').sort());
  assert.equal(sources.find(source => source.source === 'loss').location, 'history');
  assert.equal(materializePanels({runs}).length, sources.length);
  assert.deepEqual(panelDefaults('line'), {kind:'line', showRaw:true, showLegend:true});
});

test('automatic IDs use exact sources and survive visibility and title changes', () => {
  const runs = replay([event('a', 0, {loss:1, 'a.b':2, a:{b:3}}), event('b', 1, {accuracy:.9}, {run_id:'b'})]);
  const all = materializePanels({runs}), again = materializePanels({runs:[...runs].reverse()});
  assert.deepEqual(all.map(panel => panel.id), again.map(panel => panel.id));
  assert.equal(all.filter(panel => panel.source === 'a.b').length, 1);
  assert.equal(all.filter(panel => panel.source === 'a\\.b').length, 1);
  const loss = all.find(panel => panel.source === 'loss');
  assert.deepEqual(loss.runKeys, [runs.find(run=>run.id==='a').key]);
  assert.deepEqual(all.filter(panel=>panel.runKeys.includes(runs.find(run=>run.id==='b').key)).map(panel=>panel.source), ['accuracy']);
  const renamed = createPanel({...loss, title:'Training objective'});
  assert.equal(renamed.id, loss.id); assert.equal(renamed.source, 'loss');
  const other = runs.find(run => run.id === 'b');
  assert.equal(resolvePanelValues(renamed, [other])[0].missing, true);
  assert.equal(materializePanels({runs:[other], definitions:[renamed], mode:'manual'})[0].id, loss.id);
});

test('manual panels and duplicate instances stay independent when data is absent', () => {
  const settings = {smoothing:.4, nested:{enabled:true}}, first = createPanel({type:'line', source:'loss', settings});
  const duplicate = createPanel({type:first.type, source:first.source, title:'Second view', settings:first.settings});
  assert.notEqual(first.id, duplicate.id);
  settings.nested.enabled = false; duplicate.settings.nested.enabled = false;
  assert.equal(first.settings.nested.enabled, true);
  const panels = materializePanels({runs:[], definitions:[first, duplicate], mode:'manual'});
  assert.equal(panels.length, 2); assert.deepEqual(panels.map(panel => panel.id), [first.id, duplicate.id]);
  panels[0].settings.smoothing = .9; assert.equal(first.settings.smoothing, .4);
  assert.equal(materializePanels({runs:replay([event('a',0,{loss:1})]), definitions:[first]}).length, 1);
  const source = discoverPanelSources(replay([event('a',0,{loss:1})]))[0];
  assert.notEqual(createPanel(source).id, createPanel(source).id);
});

test('invalid panel types, sources, locations, modes and duplicate IDs are explicit errors', () => {
  for (const type of ['unsupported', '', undefined]) assert.throws(() => createPanel({type, source:'loss'}), /Unsupported panel type/);
  for (const source of ['', '   ', null, 42]) assert.throws(() => createPanel({type:'line', source}), /nonempty logged key/);
  assert.throws(() => createPanel({type:'line', source:'loss', location:'config'}), /source location/);
  assert.throws(() => createPanel({type:'line', source:'loss', settings:[]}), /settings/);
  assert.throws(() => createPanel({type:'line', source:'loss', title:''}), /title/);
  assert.throws(() => materializePanels({runs:[], mode:'other'}), /workspace mode/);
  assert.throws(() => materializePanels({runs:[], definitions:[{type:'line', source:'loss'}]}), /stable IDs/);
  const panel = createPanel({type:'line', source:'loss'});
  assert.throws(() => materializePanels({runs:[], definitions:[panel, panel]}), /Duplicate panel ID/);
});

test('hidden SDK axes remain available for manual creation but are not automatic', () => {
  const [run] = replay([event('a', 0, {loss:1, epoch:0})]);
  run.definitions.epoch = {hidden:true};
  assert(discoverPanelSources([run]).find(source => source.source === 'epoch').hidden);
  assert.deepEqual(materializePanels({runs:[run]}).map(panel => panel.source), ['loss']);
  const epoch = createPanel({type:'line', source:'epoch'});
  assert.equal(materializePanels({runs:[run], definitions:[epoch], mode:'manual'})[0].source, 'epoch');
});

test('older rich types remain in discovery and explicit missing snapshots do not use latest', () => {
  const [run] = replay([event('a', 0, {sample:rich('image')}), event('b', 1, {sample:rich('video')}), event('c', 2, {sample:rich('image')})]);
  assert.deepEqual(discoverPanelSources([run]).filter(source => source.source === 'sample').map(source => source.type), ['image','video']);
  const panel = createPanel({type:'image', source:'sample'});
  assert.deepEqual(panelVersions(panel, run).map(version => version.step), [0,2]);
  assert.equal(resolvePanelData(panel, run).step, 2);
  assert.equal(resolvePanelData(panel, run, {step:'0'}).step, 0);
  assert.equal(resolvePanelData(panel, run, {step:1}).missing, true);
});

test('incremental table versions reconstruct only the selected snapshot without changing history', () => {
  const table = (offset, rows) => ({_type:'table', table_id:'samples', columns:['value'], log_mode:'INCREMENTAL', row_offset:offset, data:rows});
  const [run] = replay([event('a', 0, {samples:table(0,[['first']])}), event('b', 1, {samples:table(1,[['second']])})]);
  const panel = createPanel({type:'table', source:'samples'}), original = JSON.stringify(run.history);
  assert.deepEqual(resolvePanelData(panel, run, {step:0}).value.data, [['first']]);
  assert.deepEqual(resolvePanelData(panel, run).value.data, [['first'],['second']]);
  assert.equal(JSON.stringify(run.history), original);
});

test('Summary-only rich sources have a latest value and no invented history versions', () => {
  const [run] = replay([event('a', 0, {loss:1})]);
  run.summary.report = rich('html');
  const source = discoverPanelSources([run]).find(source => source.source === 'report');
  assert.equal(source.location, 'summary');
  const panel = createPanel({...source});
  assert.deepEqual(panelVersions(panel, run), []);
  assert.equal(resolvePanelData(panel, run).location, 'summary');
  assert.equal(resolvePanelData(panel, run).value._type, 'html');
  assert.equal(resolvePanelData(panel, run, {step:0}).missing, true);
});

test('shared writer snapshots keep independent values and reject ambiguous step selection', () => {
  const [run] = replay([event('a', 0, {sample:rich('image')})]);
  run.history = [
    {_step:0, _writer:'first', _writer_label:'First', sample:{_type:'image', caption:'first'}},
    {_step:0, _writer:'second', _writer_label:'Second', sample:{_type:'image', caption:'second'}},
  ];
  const panel = createPanel({type:'image', source:'sample'});
  assert.equal(panelVersions(panel, run).length, 2);
  assert.equal(resolvePanelData(panel, run, {step:0, writer:'first'}).value.caption, 'first');
  assert.equal(resolvePanelData(panel, run, {step:0, writer:'second'}).value.caption, 'second');
  assert.throws(() => resolvePanelData(panel, run, {step:0}), /Choose a writer/);
});

test('shared writers using the same table ID never blend incremental rows', () => {
  const [run] = replay([event('a', 0, {loss:1})]);
  const table = (offset, text) => ({_type:'table', table_id:'same-id', columns:['value'], log_mode:'INCREMENTAL', row_offset:offset, data:[[text]]});
  run.history = [
    {_step:0, _writer:'first', samples:table(0,'first-0')},
    {_step:0, _writer:'second', samples:table(0,'second-0')},
    {_step:1, _writer:'first', samples:table(1,'first-1')},
    {_step:1, _writer:'second', samples:table(1,'second-1')},
  ];
  const panel = createPanel({type:'table', source:'samples'});
  assert.deepEqual(resolvePanelData(panel, run, {writer:'first'}).value.data, [['first-0'],['first-1']]);
  assert.deepEqual(resolvePanelData(panel, run, {writer:'second'}).value.data, [['second-0'],['second-1']]);
});
