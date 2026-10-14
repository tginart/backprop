/** Behavioral checks for selection and grouped chart math, including sparse
 * histories. Numeric expectations are independent hand-calculated examples.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {axisTickLabels, chartSpec, renderChart} from '../web/charts.mjs';
import {analysisFields, runField, validateFilters, matchesFilters, selectAnalysisRuns, groupRuns, aggregateSeries} from '../web/run-analysis.mjs';

const runs = [
  {key:'a', id:'a', name:'Seed 1', state:'finished', group:'train', tags:['baseline'], created_at:'2026-10-01T00:00:00Z', config:{lr:.001, model:{size:8}, 'model.size':16, seed:1}, summary:{loss:0, accuracy:.8}},
  {key:'b', id:'b', name:'Seed 2', state:'running', group:'train', tags:['candidate'], created_at:'2026-10-02T00:00:00Z', config:{lr:.01, model:{size:8}, seed:2}, summary:{loss:null}},
  {key:'c', id:'c', name:'Eval', state:'finished', tags:[], config:{lr:'0.001'}, summary:{}},
];

test('narrow accuracy and zoomed large-value axes retain distinct labels', () => {
  for (const values of [[.855,.857,.859,.861,.863],[125000,125005,125010,125015,125020],[.00001001,.00001002,.00001003]]) {
    const labels=axisTickLabels(values);
    assert.equal(new Set(labels).size,values.length);
    const parsed=labels.map(label=>label.endsWith('k') ? Number(label.slice(0,-1))*1000 : Number(label));
    assert.deepEqual(parsed,values);
  }
  const spec=chartSpec([{name:'accuracy',color:'#609cff',points:[{x:0,y:.855},{x:1,y:.863}]}]);
  const labels=[...renderChart(spec).matchAll(/text-anchor="end">([^<]+)<\/text>/g)].map(match=>match[1]);
  assert.equal(new Set(labels).size,5);
});

test('qualified fields distinguish literal dots, nested values and missing/null/zero', () => {
  assert.equal(runField(runs[0], 'config:model.size'), 8);
  assert.equal(runField(runs[0], 'config:model\\.size'), 16);
  assert.equal(runField(runs[0], 'summary:loss'), 0);
  assert.equal(runField(runs[1], 'summary:loss'), null);
  assert.equal(runField(runs[2], 'summary:loss'), undefined);
  assert.ok(analysisFields(runs).some(([key]) => key === 'config:model\\.size'));
  assert.throws(() => runField(runs[0], 'run:token'), /Unknown/);
});
test('numeric filters do not coerce strings or invent absent values', () => {
  const filter = [{field:'config:lr', op:'lte', value:'.001'}];
  assert.deepEqual(runs.filter(run => matchesFilters(run, filter)).map(run => run.key), ['a']);
  assert.throws(() => validateFilters([{...filter[0], value:'many'}]), /finite/);
  assert.throws(() => validateFilters([{...filter[0], op:'execute'}]), /comparison/);
  assert.throws(() => validateFilters([{field:'run:unknown', op:'eq', value:''}]), /Unknown/);
});
test('filters preserve explicit null and missing while matching tags and typed values', () => {
  const matching = filters => runs.filter(run => matchesFilters(run, filters)).map(run => run.key);
  assert.deepEqual(matching([{field:'summary:loss', op:'eq', value:'null'}]), ['b']);
  assert.deepEqual(matching([{field:'summary:loss', op:'missing'}]), ['c']);
  assert.deepEqual(matching([{field:'summary:loss', op:'exists'}]), ['a','b']);
  assert.deepEqual(matching([{field:'summary:loss', op:'ne', value:'0'}]), ['b']);
  assert.deepEqual(matching([{field:'run:tags', op:'contains', value:'baseline'}]), ['a']);
  assert.deepEqual(matching([{field:'run:name', op:'contains', value:'SEED'}, {field:'config:lr', op:'eq', value:'.001'}]), ['a']);
  assert.deepEqual(matching([{field:'config:lr', op:'eq', value:'"0.001"'}]), ['c']);
});
test('stable sorting keeps missing/null last in both directions and respects filters', () => {
  for (const direction of [1,-1]) assert.deepEqual(selectAnalysisRuns(runs, {sort:'summary:loss',direction}).map(run => run.key), ['a','b','c']);
  assert.deepEqual(selectAnalysisRuns(runs).map(run => run.key), ['b','a','c']);
  assert.deepEqual(selectAnalysisRuns(runs, {search:'seed', status:'finished', group:'train'}).map(run => run.key), ['a']);
  assert.deepEqual(selectAnalysisRuns(runs, {sort:'config:model.size', direction:1}).map(run => run.key), ['a','b','c']);
});
test('groups distinguish numeric/string keys, null and missing', () => {
  assert.deepEqual(groupRuns(runs, 'config:lr').map(group => group.runs.map(run => run.key)), [['a'],['b'],['c']]);
  assert.deepEqual(groupRuns(runs, 'config:model.size').map(group => group.runs.map(run => run.key)), [['a','b'],['c']]);
  assert.equal(groupRuns(runs, 'all')[0].runs.length, 3);
  assert.equal(groupRuns(runs, '')[0].key, 'a');
  assert.notEqual(groupRuns(runs, 'summary:loss')[1].key, groupRuns(runs, 'summary:loss')[2].key);
});
test('mean and population variation are independently calculated', () => {
  const series = [1,3,5].map(y => ({points:[{x:0,y}]}));
  const [point] = aggregateSeries(series);
  assert.equal(point.y, 3); assert.equal(point.count, 3);
  assert.equal(point.stddev, Math.sqrt(8/3));
  assert.deepEqual(point.band, [3 - Math.sqrt(8/3), 3 + Math.sqrt(8/3)]);
  assert.deepEqual(aggregateSeries(series, {range:'minmax'})[0].band, [1,5]);
  assert.deepEqual(aggregateSeries(series, {range:'stderr'})[0].band, [3 - Math.sqrt(8/3)/Math.sqrt(3), 3 + Math.sqrt(8/3)/Math.sqrt(3)]);
  assert.equal(aggregateSeries(series, {aggregation:'max',range:'none'})[0].y, 5);
  assert.equal(aggregateSeries(series, {aggregation:'min'})[0].y, 1);
  assert.equal(aggregateSeries(series, {aggregation:'median'})[0].y, 3);
  assert.equal(aggregateSeries([{points:[{x:0,y:2}]},{points:[{x:0,y:4}]}], {aggregation:'median'})[0].y, 3);
});
test('uneven histories never extrapolate or count one run twice at an X', () => {
  const lines = [{points:[{x:0,y:1},{x:1,y:99},{x:1,y:3},{x:2,y:NaN}]}, {points:[{x:1,y:5},{x:2,y:7}]}];
  const before = structuredClone(lines);
  assert.deepEqual(aggregateSeries(lines, {range:'minmax'}).map(({x,y,count,band}) => ({x,y,count,band})), [
    {x:0,y:1,count:1,band:[1,1]}, {x:1,y:4,count:2,band:[3,5]}, {x:2,y:7,count:1,band:[7,7]},
  ]);
  assert.deepEqual(lines, before);
  assert.throws(() => aggregateSeries(lines, {range:'confidence'}), /range/);
});

import {seriesFor} from '../web/model.mjs';
import {newLayout,arrangePanels,movePanel,setSection,setPanelOptions,panelOptions} from '../web/workspace-layout.mjs';

test('group ribbon controls numeric bounds and explicit two-axis zoom', () => {
  const line = {name:'mean',color:'#8abbff',points:[{x:0,y:4,band:[1,7]},{x:1,y:5,band:[2,8]}]};
  const spec = chartSpec([line]);
  assert(spec.yMin < 1 && spec.yMax > 8);
  assert.match(renderChart(spec), /class="variation-band"/);
  const zoomed = chartSpec([line], {zoom:[.1,.9,3,6]});
  assert.equal(zoomed.xMin,.1);assert.equal(zoomed.xMax,.9);
  assert.equal(zoomed.yMin,3);assert.equal(zoomed.yMax,6);
  assert.equal(chartSpec([line],{zoom:[0,1,6,3]}),null);
});
test('explicit nonfinite metrics break paths; unrelated sparse rows do not', () => {
  const run = {history:[{_step:0,loss:2},{_step:1,other:3},{_step:2,loss:1},{_step:3,loss:{_type:'number',value:'NaN'}},{_step:4,loss:.5}],definitions:{}};
  const points = seriesFor(run,'loss');
  assert.equal(points[1].breakBefore,undefined);assert.equal(points[2].breakBefore,true);
  const svg = renderChart(chartSpec([{name:'loss',color:'#8abbff',points}]));
  assert.match(svg,/d="M[^" ]+ L[^" ]+ M/);
});
test('section and panel moves keep metric identities and include arriving metrics', () => {
  const layout = newLayout(), metrics = ['train/loss','train/accuracy','eval/loss','validation/deep/reward'];
  setSection(layout,'custom:review',{title:'Review',custom:true,settings:{smoothing:.2}});
  movePanel(layout,metrics,'train/loss','custom:review');
  movePanel(layout,metrics,'eval/loss','train','train/accuracy');
  const arranged = arrangePanels(metrics,layout);
  assert.deepEqual(arranged.find(section=>section.id==='train').metrics,['eval/loss','train/accuracy']);
  assert.deepEqual(arranged.find(section=>section.id==='custom:review').metrics,['train/loss']);
  assert.equal(arranged.find(section=>section.id==='validation').metrics[0],'validation/deep/reward');
  setSection(layout,'custom:review',{title:'New title'});
  assert.equal(arrangePanels(metrics,layout).find(section=>section.title==='New title').metrics[0],'train/loss');
  setPanelOptions(layout,'__proto__',{span:2});assert.equal(panelOptions(layout,'__proto__').span,2);
  assert(arrangePanels([...metrics,'train/new'],layout).find(section=>section.id==='train').metrics.includes('train/new'));
  assert.throws(()=>movePanel(layout,metrics,'missing','train'),/no longer/);
});

import {captureView,restoreView,validateView} from '../web/saved-views.mjs';
test('named view snapshot restores only display state for its owning project', () => {
  const state = {project:'team/project',xAxis:'_step',smoothing:.4,logScale:false,columns:3,runColumns:['config:lr'],search:'seed',status:'',group:'',
    selected:new Set(['team/project/a','other/project/a']),hiddenRuns:new Set(['team/project/a','other/project/a']),panelSettings:new Map([['["team/project","loss"]',{title:'Loss'}],['["other/project","loss"]',{title:'Other'}]]),
    hiddenPanels:new Set(),pinnedPanels:new Set(),collapsedSections:new Set(),analysisPreferences:new Map([['team/project',{filters:[],groupBy:'config:lr'}]]),
    workspaceLayouts:new Map([['team/project',newLayout()]]),runColors:new Map([['team/project/a','#8abbff'],['other/project/a','#ff0000']]),zoom:new Map(),hiddenSeries:new Map()};
  const snapshot = captureView(state,['team/project/a']);
  assert.deepEqual(snapshot.hiddenRuns,['team/project/a']);assert.equal(Object.hasOwn(snapshot,'selected'),false);assert.equal(snapshot.panelSettings.length,1);
  state.panelSettings.get('["team/project","loss"]').title='Changed';state.smoothing=0;state.selected.delete('team/project/a');state.hiddenRuns.delete('team/project/a');
  restoreView(state,snapshot,['team/project/a']);
  assert.equal(state.smoothing,.4);assert.equal(state.panelSettings.get('["team/project","loss"]').title,'Loss');
  assert.equal(state.panelSettings.get('["other/project","loss"]').title,'Other');
  assert(state.hiddenRuns.has('other/project/a') && state.hiddenRuns.has('team/project/a'));
  assert.deepEqual([...state.selected],['other/project/a']);
  const invalid={...snapshot,columns:9};assert.throws(()=>validateView(invalid,state.project),/unsupported/);
  assert.throws(()=>validateView(snapshot,'wrong/project'),/different project/);
});

import {smoothSeries} from '../web/smoothing.mjs';
test('EMA debias, time weighting, group variation and gaps use independent expectations',()=>{
  const points=[{x:0,y:4},{x:1,y:2},{x:2,y:8}];
  assert.deepEqual(smoothSeries(points,{smoothing:.5}).map(point=>point.y),[4,8/3,40/7]);
  const time=smoothSeries([{x:0,y:0},{x:1,y:10},{x:10,y:10}],{smoothing:.25,smoothingMethod:'twema',viewportScale:10});
  assert.equal(time[1].y,20/3);assert(time[2].y>9.98);
  const gap=smoothSeries([{x:0,y:10},{x:5,y:1,breakBefore:true}],{smoothing:.9});assert.equal(gap[1].y,1);
  const grouped=aggregateSeries([{points:[{x:0,y:2},{x:2,y:4,breakBefore:true}]},{points:[{x:0,y:4},{x:2,y:8,breakBefore:true}]}]);
  assert.equal(grouped[1].breakBefore,true);assert.deepEqual(grouped[1].band,[4,8]);
  const smoothed=smoothSeries(grouped,{smoothing:.8});assert.deepEqual(smoothed[1].band,[4,8]);assert.equal(smoothed[1].raw,6);
  assert.deepEqual(points,[{x:0,y:4},{x:1,y:2},{x:2,y:8}]);
});

test('section pinning and hiding preserve assignments for restoration',()=>{
  const layout=newLayout(),metrics=['train/z','train/a','eval/loss'];
  setSection(layout,'eval',{pinned:true,page:1});
  assert.equal(arrangePanels(metrics,layout)[0].id,'eval');
  setSection(layout,'eval',{hidden:true});assert(!arrangePanels(metrics,layout).some(section=>section.id==='eval'));
  setSection(layout,'eval',{hidden:false});assert.deepEqual(arrangePanels(metrics,layout)[0].metrics,['eval/loss']);
  layout.panelOrder=['train/a','train/z'];
  assert.deepEqual(arrangePanels(metrics,layout).find(section=>section.id==='train').metrics,['train/a','train/z']);
});

test('regex run search is explicit and invalid patterns never silently match',()=>{
  const runs=[{name:'seed-01',id:'a',tags:[],created_at:'2026-01-01'},{name:'seed-12',id:'b',tags:[],created_at:'2026-01-02'}];
  assert.deepEqual(selectAnalysisRuns(runs,{search:'^seed-0[1-3]',searchRegex:true}).map(run=>run.id),['a']);
  assert.deepEqual(selectAnalysisRuns(runs,{search:'^seed-0[1-3]'}),[]);
  assert.throws(()=>selectAnalysisRuns(runs,{search:'[',searchRegex:true}),SyntaxError);
});
