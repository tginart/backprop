/**
 * Architecture: realistic app display maps -> JSON snapshot -> fresh project
 * state. Tests distinguish display choices from navigation and checkbox state.
 * Main callers: node --test. No browser storage, network or credentials are used.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {captureView,defaultView,validateView,restoreView,migrateLocalView} from '../web/saved-views.mjs';

const project='team/project', run=`${project}/run-one`, other='team/other/run-one';
const output=`${run}:writer-one:samples`, panelOutput=JSON.stringify(['panel]one',run,'writer-one']);
function state() {
  const value={project,selected:new Set([run]),hiddenRuns:new Set(),panelSettings:new Map(),hiddenPanels:new Set(),pinnedPanels:new Set(),collapsedSections:new Set(),analysisPreferences:new Map(),workspaceLayouts:new Map(),runColors:new Map(),richSteps:new Map(),tableOptions:new Map(),tableFilters:new Map(),zoom:new Map([['loss',[1,2,3,4]]]),hiddenSeries:new Map([['loss',new Set([run])]]),tablePages:new Map([[output,2]]),runPage:3,runKey:run,view:'runs'};
  restoreView(value,defaultView(project),[run],{preserveTransient:true});
  return value;
}

test('defaults are complete and clear searches, smoothing and layouts from a previous project',()=>{
  const first=defaultView(project), second=defaultView('team/other');
  first.layout.sections.push({id:'review'});first.analysis.filters.push({field:'run:name',op:'eq',value:'test'});
  assert.deepEqual(second,validateView(second,'team/other'));
  assert.deepEqual(second.layout.sections,[]);assert.deepEqual(second.analysis.filters,[]);
  const value=state();Object.assign(value,{xAxis:'config:epoch',smoothing:.8,search:'old',metricSearch:'loss',richSearch:'pictures',compareSearch:'seed',metricRegex:true});
  restoreView(value,defaultView(project),[run]);
  assert.equal(value.xAxis,'defined');assert.equal(value.smoothing,0);
  for(const key of ['search','metricSearch','richSearch','compareSearch'])assert.equal(value[key],'');
  assert.equal(value.metricRegex,false);assert.equal(value.selected.has(run),true);
});

test('capture persists searches, independent visibility, stable rich steps and table display settings',()=>{
  const value=state();Object.assign(value,{metricSearch:'^loss',metricRegex:true,richRun:run,richType:'table',richSearch:'samples',compareSearch:'seed',differences:true});
  value.hiddenRuns.add(run);value.hiddenRuns.add(other);
  value.richSteps.set(output,'7');value.richSteps.set(panelOutput,'9');value.richSteps.set(`${other}:single:samples`,'2');
  value.tableOptions.set(`${panelOutput}:0:1`,{sort:1,direction:-1,hiddenColumns:[2],column:'0',limit:25});
  value.tableOptions.set(`summary:${run}:evaluation`,{limit:100});
  value.tableOptions.set('artifact-table-1',{limit:25});
  value.tableFilters.set(output,'good');value.tableFilters.set('linked-table-2','temporary');
  value.panelSettings.set(JSON.stringify([project,'loss']),{smoothing:undefined,title:'Loss'});
  const snapshot=captureView(value,[run]);
  assert.deepEqual(snapshot.hiddenRuns,[run]);assert.equal(Object.hasOwn(snapshot,'selected'),false);
  assert.deepEqual(snapshot.richSteps,[[output,'7'],[panelOutput,'9']]);
  assert.equal(snapshot.tableOptions.length,2);assert.deepEqual(snapshot.tableFilters,[[output,'good']]);
  assert.deepEqual(snapshot.panelSettings[0][1],{title:'Loss'});
  assert.deepEqual(JSON.parse(JSON.stringify(snapshot)),snapshot);
  const restored=state();restored.selected.clear();restored.richSteps.set(`${other}:single:samples`,'2');
  restoreView(restored,snapshot,[run]);
  for(const key of ['metricSearch','metricRegex','richRun','richType','richSearch','compareSearch','differences'])assert.deepEqual(restored[key],value[key]);
  assert.equal(restored.selected.size,0);assert.equal(restored.hiddenRuns.has(run),true);
  assert.equal(restored.richSteps.get(panelOutput),'9');assert.equal(restored.richSteps.get(`${other}:single:samples`),'2');
  assert.deepEqual(restored.tableOptions.get(`${panelOutput}:0:1`),{sort:1,direction:-1,hiddenColumns:[2],column:'0',limit:25});
});

test('save application can preserve transient state and view switches reset it',()=>{
  const value=state(), selected=value.selected, zoom=value.zoom, pages=value.tablePages;
  restoreView(value,defaultView(project),[run],{preserveTransient:true});
  assert.equal(value.runPage,3);assert.equal(zoom.size,1);assert.equal(pages.get(output),2);assert.equal(value.hiddenSeries.size,1);
  assert.equal(value.selected,selected);assert.equal(value.runKey,run);assert.equal(value.view,'runs');
  restoreView(value,defaultView(project),[run]);
  assert.equal(value.runPage,0);assert.equal(zoom.size,0);assert.equal(pages.size,0);assert.equal(value.hiddenSeries.size,0);assert.equal(value.selected,selected);
});

test('existing hiddenRuns snapshots receive new defaults while selected-only local imports are explicit',()=>{
  const old=defaultView(project);
  for(const key of ['project','metricSearch','metricRegex','richRun','richType','richSearch','compareSearch','differences','richSteps','tableOptions','tableFilters'])delete old[key];
  assert.equal(validateView(old,project).metricSearch,'');
  const {hiddenRuns:ignored,...legacy}=old;legacy.selected=[run];
  assert.throws(()=>validateView(legacy,project),/explicit visibility migration/);
  const result=migrateLocalView(legacy,project,[run,`${project}/run-two`,other]);
  assert.deepEqual(result.snapshot.hiddenRuns,[`${project}/run-two`]);assert.equal(Object.hasOwn(result.snapshot,'selected'),false);
  assert.match(result.warning,/still require a Foundry save/);
  assert.match(result.warning,/absent during import remain visible/);
});

test('invalid snapshots fail before mutating current state or silently changing JSON values',()=>{
  const value=state(), snapshot=defaultView(project), before=captureView(value,[run]);
  for(const bad of [
    {...snapshot,hiddenRuns:[other]}, {...snapshot,colors:[[other,'#ffffff']]},
    {...snapshot,richSteps:[['artifact-table-1','2']]}, {...snapshot,selected:[run]},
    {...snapshot,auth:{token:'test-only'}}, {...snapshot,layout:{...snapshot.layout,panelOptions:[['loss',null]]}},
    {...snapshot,tableOptions:[[output,{limit:0}]]}, {...snapshot,analysis:{...snapshot.analysis,value:undefined}},
    {...snapshot,smoothing:NaN}, {...snapshot,smoothing:Infinity},
  ])assert.throws(()=>restoreView(value,bad,[run]));
  assert.deepEqual(captureView(value,[run]),before);
  value.panelSettings.set(JSON.stringify([project,'loss']),{bounds:[undefined]});
  assert.throws(()=>captureView(value,[run]),/non-JSON/);
});
