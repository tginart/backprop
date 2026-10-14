/** Frontend contract tests: event replay, SDK semantics, and chart data fidelity.
 * Main caller: node --test tests/test_frontend_model.mjs. No browser required.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeEvents, seriesFor, metricKeys, tableAtStep, escapeHtml, csv, loggedValues, flatten, pathKey, keyPath, valueAtKey } from '../web/model.mjs';
import { chartSpec, decimate } from '../web/charts.mjs';
const event = (id, type, payload, extra = {}) => ({ event_id: `event-${id}`, run_id: 'r1', entity: 'team', project: 'demo', timestamp: new Date(1700000000000 + id * 1000).toISOString(), type, payload, ...extra });

test('nested metrics retain literal dotted/backslash identities when collisions arrive later', () => {
  const rows=[event(0,'metric_definition',{name:'train.loss',step_metric:'train.epoch'}),
    event(1,'history',{step:0,metrics:{train:{loss:4,epoch:1}}}),
    event(2,'history',{step:1,metrics:{train:{loss:2,epoch:2},'train.loss':20,'train.epoch':10,'back\\slash':7}})];
  const first=normalizeEvents(rows.slice(0,2)).runs[0], run=normalizeEvents(rows).runs[0];
  assert.deepEqual(metricKeys([first]),['train.epoch','train.loss']);
  assert.deepEqual(metricKeys([run]),['back\\\\slash','train.epoch','train.loss','train\\.epoch','train\\.loss']);
  assert.deepEqual(seriesFor(run,'train.loss',{xAxis:'defined'}).map(p=>[p.x,p.y]),[[1,4],[10,2]],'native declared axes prefer literal keys');
  assert.deepEqual(seriesFor(run,'train.loss',{xAxis:'train.epoch'}).map(p=>[p.x,p.y]),[[1,4],[2,2]],'explicit nested axis uses its exact path');
  assert.deepEqual(seriesFor(run,'train\\.loss',{xAxis:'train\\.epoch'}).map(p=>[p.x,p.y]),[[10,20]]);
  assert.equal(run.summary.train.loss,2); assert.equal(run.summary['train.loss'],20);
  for (const path of [['train.loss'],['train','loss'],['a\\b','.c'],['','x']]) assert.deepEqual(keyPath(pathKey(path)),path);
});

test('nested rich history keeps snapshots and arrays atomic without treating descriptor fields as metrics', () => {
  const table=(offset,data)=>({_type:'table',table_id:'nested-table',columns:['x'],data,log_mode:'INCREMENTAL',row_offset:offset});
  const image={_type:'image',path:'image.png',width:100};
  const run=normalizeEvents([event(0,'history',{step:0,metrics:{outputs:{table:table(0,[[1]]),gallery:[image],note:'one'},'outputs.table':{_type:'histogram',values:[1],bins:[0,1]}}}),
    event(1,'history',{step:1,metrics:{outputs:{table:table(1,[[2]]),note:'two'}}})]).runs[0];
  assert.deepEqual(metricKeys([run]),[]);
  assert.deepEqual(run.rich['outputs.table'].value.data,[[1],[2]]);
  assert.deepEqual(tableAtStep(run,'outputs.table',0).data,[[1]]);
  assert.equal(tableAtStep(run,'outputs\\.table',0)._type,'histogram');
  assert.deepEqual(run.rich['outputs.gallery'].value,[image]);
  assert.equal(run.rich['outputs.note'].value,'two');
  assert.equal(loggedValues(run).filter(value=>value.key==='outputs.table').length,1);
  assert(loggedValues(run).every(value=>value.source==='history'));
});

test('nested rewind and summary replay do not mutate cached events or collapse key collisions', () => {
  const rows=[event(0,'history',{step:0,metrics:{train:{loss:3}},summary:{train:{loss:3}}}),
    event(1,'history',{step:1,metrics:{train:{loss:2}},summary:{train:{loss:2},'train.loss':9}}),
    event(2,'history_truncate',{step:0,summary:{train:{loss:3}}}),event(3,'history',{step:1,metrics:{train:{loss:1}}})];
  const before=JSON.stringify(rows),run=normalizeEvents(rows).runs[0];
  assert.deepEqual(seriesFor(run,'train.loss').map(p=>p.y),[3,1]);
  assert.equal(run.summary.train.loss,1); assert.equal(JSON.stringify(rows),before);
  const values=new Map(flatten({train:{loss:1},'train.loss':2,'train\\loss':3}));
  assert.equal(values.size,3); assert.equal(values.get('train.loss'),1); assert.equal(values.get('train\\.loss'),2);
  assert.equal(valueAtKey(JSON.parse('{"__proto__":{"value":8}}'),'__proto__.value'),8); assert.equal({}.value,undefined);
});

test('summary-only rich values remain discoverable without invented history or duplicate cards', () => {
  const image={_type:'image',path:'blobs/image.png'}, histogram={_type:'histogram',values:[1],bins:[0,1]};
  const run=normalizeEvents([event(0,'history',{step:0,metrics:{existing:image}}),event(1,'summary',{values:{existing:image,preview:image,nested:{distribution:histogram},gallery:[image]}})]).runs[0];
  assert.deepEqual(loggedValues(run).map(value=>[value.key,value.source]),[['existing','history'],['preview','summary'],['nested.distribution','summary'],['gallery','summary']]);
  assert.equal(loggedValues(run)[1].step,undefined);
  delete run.summary.preview; assert(!loggedValues(run).some(value=>value.key==='preview'));
  const exported=csv([['object','array'],[image,[1,2]]]);
  assert(!exported.includes('[object Object]')); assert(exported.includes('""_type"":""image""')); assert(exported.includes('[1,2]'));
});

test('matching artifact content enriches absent provenance but rejects conflicting known origins', () => {
  const manifest={artifact_id:'a',version:'sha256-v',name:'table',files:[{name:'table.json',sha256:'a'.repeat(64),size:4,path:'first',mime_type:null}]};
  const known={...manifest,files:[{...manifest.files[0],path:'second',mime_type:'application/json',md5:'transport',ref:null,birth_artifact_id:'original'}]};
  const model=normalizeEvents([event(0,'artifact',manifest),event(1,'artifact',known)]);
  assert.deepEqual(model.issues,[]); assert.equal(model.runs[0].artifacts[0].files[0].path,'first');
  assert.equal(model.runs[0].artifacts[0].files[0].birth_artifact_id,'original'); assert.equal(model.runs[0].artifacts[0].files[0].ref,null);
  assert.equal(manifest.files[0].birth_artifact_id,undefined);
  for(const file of [{...known.files[0],ref:'backprop-artifact://other/file'},{...known.files[0],birth_artifact_id:'other'}]) {
    const changed=normalizeEvents([event(0,'artifact',manifest),event(1,'artifact',known),event(2,'artifact',{...known,files:[file]})]);
    assert.match(changed.issuesByProject['team/demo'][0],/Conflicting immutable/); assert.equal(changed.runs[0].artifacts[0].files[0].birth_artifact_id,'original');
  }
  const incomplete={...manifest,files:[{...manifest.files[0],size:undefined}]};
  assert.match(normalizeEvents([event(0,'artifact',incomplete),event(1,'artifact',{...incomplete,files:[{...incomplete.files[0],path:'changed'}]})]).issues[0],/Conflicting immutable/);
});

test('unsupported legacy formats stay explicit and warnings are scoped by entity/project', () => {
  const source=[event(0,'artifact',{name:'a',artifact_id:'a',version:'v1',files:[],metadata:{original:true}}),
    event(1,'artifact_metadata',{artifact_id:'a',metadata_revision:1,metadata:{changed:true}},{project:'legacy'}),
    event(2,'future_format',{}, {entity:'another-team'}),
    {event_id:'bad',payload:'{'},
    event(3,'history','{',{project:'legacy'})];
  const model=normalizeEvents(source);
  assert.deepEqual(model.issuesByProject['team/demo'],[]);
  assert.deepEqual(model.issuesByProject['team/legacy'],['An event could not be decoded.','Unsupported event type: artifact_metadata']);
  assert.deepEqual(model.issuesByProject['another-team/demo'],['Unsupported event type: future_format']);
  assert.deepEqual(model.globalIssues,['An event could not be decoded.']);
  assert.deepEqual(model.runs.find(run=>run.project==='demo'&&run.entity==='team').artifacts[0].metadata,{original:true});
  assert(model.issues.includes('Unsupported event type: artifact_metadata'));
});

test('atomic Table artifact manifests retain exact run/version identity beside explicit artifacts', () => {
  const manifest = (id, version, name='run-r1-results') => ({name,artifact_id:id,version,artifact_type:'run_table',files:[{name:'results.table.json',path:`blobs/${id}-${version}`}]});
  const first=manifest('a','sha256-first'), second=manifest('b','sha256-second');
  const rows=[event(-1,'run_start',{config:{initial:true},artifacts:[manifest('initial','sha256-initial')]}), event(0,'history',{step:0,metrics:{loss:1},artifacts:[first]}),
    event(1,'summary',{values:{score:.9},artifacts:[{...first,created_at:'later publication'}]}),
    event(2,'history',{step:1,metrics:{loss:.5},artifacts:[second]}),
    event(3,'artifact',manifest('explicit','sha256-explicit','model')),
    event(4,'config',{values:{table_key:'value'},artifacts:[manifest('config','sha256-config')]}),
    event(5,'summary',{values:{},artifacts:[first]},{run_id:'r2'}),
    event(6,'history',{step:0,metrics:{},artifacts:[first]},{project:'another'}),
    event(7,'summary',{values:{},artifacts:[manifest('a','sha256-new-version')]})];
  const model=normalizeEvents([...rows,rows[0]]), run=model.runs.find(run=>run.id==='r1'&&run.project==='demo');
  assert.deepEqual(run.artifacts.map(value=>[value.artifact_id,value.version]),[['initial','sha256-initial'],['a','sha256-first'],['b','sha256-second'],['explicit','sha256-explicit'],['config','sha256-config'],['a','sha256-new-version']]);
  assert.equal(run.artifacts[1].description,undefined);
  assert.equal(run.artifacts[1].timestamp,rows[1].timestamp);
  assert.equal(model.runs.find(run=>run.id==='r2').artifacts.length,1);
  assert.equal(model.runs.find(run=>run.project==='another').artifacts.length,1);
  assert.equal(run.history.length,2); assert.equal(run.config.table_key,'value');
  assert.deepEqual(model.issues,[]);
});

test('immutable manifest comparison ignores ordering and SHA-identical transport changes but warns on conflicts in the owning project', () => {
  const file={name:'results.table.json',sha256:'a'.repeat(64),size:12,digest:'md5-value',mime_type:'application/json',dataset_rid:'dataset-one',path:'blobs/old',branch:'master',extra:{format:'table',revision:1}};
  const manifest={artifact_id:'stable-id',version:'sha256-version',name:'results',artifact_type:'run_table',description:'original',metadata:{optimizer:{lr:.1,seed:1},labels:['a','b']},files:[file,{name:'notes.txt',sha256:'b'.repeat(64),size:5,path:'blobs/notes'}],created_at:'original-time',history_step:0,state:'COMMITTED'};
  const reordered={...manifest,metadata:{labels:['a','b'],optimizer:{seed:1,lr:.1}},files:[manifest.files[1],{...file,extra:{revision:1,format:'table'}}]};
  const moved={...reordered,created_at:'later-time',history_step:10,state:'PENDING',files:[manifest.files[1],{...file,sha256:'A'.repeat(64),dataset_rid:'dataset-two',path:'blobs/new',branch:'another'}]};
  const baseline=[event(0,'history',{step:0,metrics:{},artifacts:[manifest]}),event(1,'summary',{values:{},artifacts:[reordered]}),event(2,'artifact',moved)];
  const same=normalizeEvents(baseline);
  assert.deepEqual(same.issues,[]); assert.equal(same.runs[0].artifacts.length,1);
  assert.equal(same.runs[0].artifacts[0].files[0].path,'blobs/old');
  for (const changed of [{...manifest,metadata:{optimizer:{lr:.2,seed:1},labels:['a','b']}}, {...manifest,files:[{...file,sha256:'c'.repeat(64)},manifest.files[1]]}, {...manifest,files:[{...file,size:13},manifest.files[1]]}]) {
    const model=normalizeEvents([...baseline,event(3,'artifact',changed),event(4,'artifact',changed,{project:'other'}),event(5,'run_start',{}, {entity:'another-team'})]);
    assert.equal(model.runs.find(run=>run.project==='demo'&&run.entity==='team').artifacts.length,1);
    assert.equal(model.runs.find(run=>run.project==='demo'&&run.entity==='team').artifacts[0].metadata.optimizer.lr,.1);
    assert.equal(model.issuesByProject['team/demo'].length,1);
    assert.match(model.issuesByProject['team/demo'][0],/Conflicting immutable artifact manifest.*Showing the first/);
    assert.deepEqual(model.issuesByProject['team/other'],[]); assert.deepEqual(model.issuesByProject['another-team/demo'],[]); assert.deepEqual(model.globalIssues,[]);
  }
  const noChecksum={...manifest,files:[{name:'unverified.txt',path:'blobs/first'}]};
  const unverified=normalizeEvents([event(0,'artifact',noChecksum),event(1,'artifact',{...noChecksum,files:[{name:'unverified.txt',path:'blobs/second'}]})]);
  assert.match(unverified.issues[0],/Conflicting immutable artifact manifest/);
});

test('console terminal edits replace Unicode characters across same-stream chunks only', () => {
  const rows = [event(0, 'run_start', {}), event(1, 'console', { text: 'progress 1' }),
    event(2, 'console', { text: 'warning\n', stream: 'stderr' }),
    event(3, 'console', { replace_tail: 1, text: '2\n' }),
    event(4, 'console', { text: 'old🙂' }), event(5, 'console', { text: 'x' }),
    event(6, 'console', { replace_tail: 2, text: 'new' })];
  const { runs: [run], issues } = normalizeEvents(rows);
  assert.equal(run.console.filter(row => row.stream === 'stdout').map(row => row.text).join(''), 'progress 2\noldnew');
  assert.equal(run.console.filter(row => row.stream === 'stderr').map(row => row.text).join(''), 'warning\n');
  assert.equal(run.console[0].timestamp, rows[1].timestamp);
  assert.deepEqual(issues, []);
  assert.equal(normalizeEvents([...rows, event(7, 'console', { replace_tail: -1, text: 'bad' })]).issues.length, 1);
});

test('archive/live replay deduplicates events and scopes run identity by project', () => {
  const first = event(0, 'run_start', { name: 'baseline', config: { rate: .1 } });
  const rows = [first, event(1, 'history', { step: 0, metrics: { loss: 3 } }), first, event(2, 'history', { step: 1, metrics: { loss: 2 } }), event(3, 'run_finish', { state: 'finished' }), event(4, 'run_start', {}, { project: 'other' })];
  const model = normalizeEvents(rows);
  assert.equal(model.runs.length, 2); assert.equal(model.eventCount, 5);
  const run = model.runs.find(run => run.project === 'demo');
  assert.equal(run.name, 'baseline'); assert.equal(run.state, 'finished'); assert.equal(run.summary.loss, 2); assert.equal(run.history.length, 2);
  assert.deepEqual(model.issues, []);
});

test('native stream records, deleted keys, console, and unsafe property names preserve semantics', () => {
  const start = event(0, 'run_start', { config: JSON.parse('{"__proto__":{"polluted":true},"temporary":1}') });
  const config = event(1, 'config', { values: { stable: 2 }, deleted: ['temporary'] });
  const history = event(2, 'history', { step: 0, metrics: JSON.parse('{"__proto__":8,"constructor":9,"loss":1}') });
  const summary = event(3, 'summary', { values: { winner: true }, deleted: ['loss'] });
  const log = event(4, 'console', { text: '<script>alert(1)</script>', stream: 'stderr' });
  const records = [start, config, history, summary, log].map(({ type, payload, ...rest }) => ({ ...rest, event_type: type, payload: JSON.stringify(payload) }));
  const run = normalizeEvents(records).runs[0];
  assert.equal({}.polluted, undefined); assert.equal(run.config.__proto__.polluted, true);
  assert.equal(run.config.temporary, undefined); assert.equal(run.config.stable, 2);
  assert.equal(run.summary.loss, undefined); assert.equal(run.history[0].__proto__, 8); assert.equal(run.summary.constructor, 9);
  assert.equal(run.console[0].stream, 'stderr'); assert.equal(run.console.length, 1);
});

test('incremental tables reconstruct old snapshots without duplicating rows', () => {
  const table = (offset, data) => ({ _type: 'table', columns: ['prediction'], data, table_id: 't1', log_mode: 'INCREMENTAL', row_offset: offset });
  const first = event(1, 'history', { step: 1, metrics: { samples: table(0, [['cat'], ['dog']]) } });
  const last = event(2, 'history', { step: 2, metrics: { samples: table(2, [['owl']]) } });
  const run = normalizeEvents([event(0, 'run_start', {}), first, last, last]).runs[0];
  assert.deepEqual(run.rich.samples.value.data, [['cat'], ['dog'], ['owl']]);
  assert.deepEqual(tableAtStep(run, 'samples', 1).data, [['cat'], ['dog']]);
  assert.deepEqual(tableAtStep(run, 'samples', 2).data, [['cat'], ['dog'], ['owl']]);
  assert.deepEqual(run.history[1].samples.data, [['owl']], 'history stores the delta, not another copy of the full table');
});

test('custom step axes, wildcard definitions, and live summary aggregates match SDK', () => {
  const run = normalizeEvents([
    event(0, 'run_start', {}), event(1, 'metric_definition', { name: 'train/*', step_metric: 'tokens', summary: 'min,max,mean', goal: 'minimize' }),
    event(2, 'metric_definition', { name: 'tokens', hidden: true }),
    event(3, 'history', { step: 0, metrics: { 'train/loss': 4, tokens: 100 } }),
    event(4, 'history', { step: 1, metrics: { 'train/loss': 2, tokens: 200 } }),
    event(5, 'history', { step: 2, metrics: { 'train/loss': 0 } }),
  ]).runs[0];
  assert.deepEqual(metricKeys([run]), ['train/loss']);
  assert.deepEqual(metricKeys([run], true), ['tokens', 'train/loss']);
  assert.deepEqual(run.summary['train/loss'], { min: 0, max: 4, mean: 2 });
  assert.deepEqual(seriesFor(run, 'train/loss', { xAxis: 'defined', smoothing: .5 }).map(p => [p.x, p.y]), [[100, 4], [200, 8/3]]);
  assert.equal(seriesFor(run, 'train/loss', { logScale: true }).length, 2);
});

test('dense charts retain isolated extrema and support more than a JS argument limit', () => {
  const points = Array.from({ length: 250000 }, (_, x) => ({ x, y: x === 12345 ? 1000 : 1 }));
  const reduced = decimate(points);
  assert(reduced.some(p => p.y === 1000)); assert(reduced.length < 800);
  const spec = chartSpec([{ name: 'large', color: '#b9e778', points }]);
  assert.equal(spec.xMax, 249999); assert(spec.yMax > 1000);
});

test('exact definitions inherit axes from wildcards and nested plot tables accumulate', () => {
  const plot = (offset, data) => ({ _type: 'plot', kind: 'line', table: { _type: 'table', table_id: 'nested', log_mode: 'INCREMENTAL', columns: ['x', 'y'], data, row_offset: offset }, fields: { x: 'x', y: 'y' } });
  const run = normalizeEvents([
    event(0, 'run_start', {}), event(1, 'metric_definition', { name: '*', hidden: false }),
    event(2, 'metric_definition', { name: 'train/*', step_metric: 'tokens' }),
    event(3, 'metric_definition', { name: 'train/loss', summary: 'min' }),
    event(4, 'history', { step: 0, metrics: { 'train/loss': 3, tokens: 100, trajectory: plot(0, [[100, 3]]) } }),
    event(5, 'history', { step: 1, metrics: { 'train/loss': 2, tokens: 200, trajectory: plot(1, [[200, 2]]) } }),
  ]).runs[0];
  assert.deepEqual(seriesFor(run, 'train/loss', { xAxis: 'defined' }).map(p => p.x), [100, 200]);
  assert.deepEqual(run.summary['train/loss'], { min: 2 });
  assert.deepEqual(run.rich.trajectory.value.table.data, [[100, 3], [200, 2]]);
  assert.deepEqual(tableAtStep(run, 'trajectory', 0).table.data, [[100, 3]]);
});

test('bar domains retain entire edge bins and zero baseline for negative values', () => {
  const spec = chartSpec([{ name: 'negative', color: '#b9e778', points: [{ x: .5, y: -2, width: 1 }, { x: 1.5, y: -3, width: 1 }] }], { kind: 'bar' });
  assert.equal(spec.xMin, 0); assert.equal(spec.xMax, 2); assert(spec.yMax > 0); assert(spec.yMin < -3);
});

test('logged strings are escaped and CSV survives delimiters and multiline values', () => {
  assert.equal(escapeHtml('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  assert.equal(csv([['a,b', 'line\nnext', 'a"b']]), '"a,b","line\nnext","a""b"');
});

test('metadata updates preserve run lifecycle and file records replace only the matching name', () => {
  const start = event(0, 'run_start', { name: 'old', notes: 'old notes', tags: ['old'] });
  const run = normalizeEvents([start, event(1, 'run_finish', { state: 'finished' }),
    event(2, 'run_metadata', { values: { name: 'new', tags: ['new'], group: 'group' }, deleted: ['notes'] }),
    event(3, 'file', { name: 'output.txt', file: { path: 'v1', size: 10 }, policy: 'now' }),
    event(4, 'file', { name: 'other.txt', file: { path: 'other', size: 20 }, policy: 'end' }),
    event(5, 'file', { name: 'output.txt', file: { path: 'v2', size: 30 }, policy: 'live' }),
  ]).runs[0];
  assert.equal(run.name, 'new'); assert.deepEqual(run.tags, ['new']); assert.equal(run.notes, '');
  assert.equal(run.created_at, start.timestamp); assert.equal(run.state, 'finished');
  assert.equal(run.files.length, 2); assert.equal(run.files[0].path, 'v2'); assert.equal(run.files[1].path, 'other');
});

test('authoritative history summaries replace stale aggregates, including empty snapshots', () => {
  const run = normalizeEvents([event(0, 'run_start', {}), event(1, 'history', { step: 0, metrics: { loss: 4 }, summary: { loss: { min: 4 } } }),
    event(2, 'history', { step: 1, metrics: { loss: 2 }, summary: {} }),
    event(3, 'history', { step: 2, metrics: { loss: { _type: 'number', value: 'NaN' } }, summary: { special: { _type: 'number', value: 'NaN' } } }),
  ]).runs[0];
  assert.deepEqual(Object.keys(run.summary), ['special']); assert.equal(run.summary.special.value, 'NaN');
  assert.deepEqual(seriesFor(run, 'loss').map(point => point.y), [4, 2]);
});

test('rewind removes future scalars and nested incremental table rows before continuation', () => {
  const table = (offset, value) => ({_type:'table',table_id:'t',columns:['x'],log_mode:'INCREMENTAL',row_offset:offset,data:[[value]]});
  const run = normalizeEvents([event(0,'run_start',{}),
    event(1,'history',{step:0,metrics:{loss:4,table:table(0,'kept')}}),
    event(2,'history',{step:1,metrics:{loss:2,table:table(1,'removed'),future:'removed'}}),
    event(3,'history_truncate',{step:0,summary:{loss:4}}),
    event(4,'history',{step:1,metrics:{loss:3,table:table(1,'replacement')},summary:{loss:3}}),
  ]).runs[0];
  assert.deepEqual(run.history.map(row=>row.loss),[4,3]);
  assert.deepEqual(run.rich.table.value.data,[['kept'],['replacement']]);
  assert.equal(run.rich.future,undefined); assert.equal(run.summary.loss,3);
});


test('config pin metadata is retained and deletion clears pins', () => {
  const rows = [event(0, 'run_start', {}), event(1, 'run_metadata', {values:{pinned_config_keys:['seed','missing',4]}})];
  assert.deepEqual(normalizeEvents(rows).runs[0].pinned_config_keys, ['seed','missing']);
  assert.deepEqual(normalizeEvents([...rows,event(2,'run_metadata',{deleted:['pinned_config_keys']})]).runs[0].pinned_config_keys, []);
});


test('workspace revisions never create runs or experiment replay issues',()=>{
  const view=event(3,'workspace_snapshot','invalid experiment JSON',{run_id:'__workspace__:test'});
  const result=normalizeEvents([event(1,'run_start',{name:'Training'}),view,event(2,'history',{step:0,metrics:{loss:1}})]);
  assert.equal(result.runs.length,1);assert.equal(result.runs[0].history.length,1);assert.deepEqual(result.issues,[]);
});
