/** Actual sklearn outputs retain identical chart/table values in browser reads. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {normalizeEvents,flatten,keyPath} from '../web/model.mjs';
const path=process.env.BACKPROP_SKLEARN_REPLAY||'test-results/sklearn-training-replay.json';
test('all sklearn helpers survive Python/browser duplicate and reordered replay',t=>{
 const fixture=JSON.parse(fs.existsSync(path)?fs.readFileSync(path,'utf8'):execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/sklearn_replay_fixture.py'],{encoding:'utf8',maxBuffer:8*1024*1024,env:{...process.env,PYTHONPATH:'sdk'}}));
 if(fixture.unavailable){t.skip(fixture.unavailable);return;}
 assert.equal(fixture.helpers.length,15);
 const result=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
 assert.deepEqual(result.issues,[]);assert.equal(result.runs.length,1);
 const run=result.runs[0];assert.equal(run.state,fixture.expected.state);
 const clean=row=>Object.fromEntries(flatten(row).map(([key,value])=>[keyPath(key).join('.'),value]).filter(([key])=>!['_timestamp','_runtime'].includes(key)));
 assert.deepEqual(run.history.map(clean),fixture.expected.history.map(clean));
 const latest=key=>run.history.filter(row=>row[key]).at(-1)[key];
 assert.equal(latest('feature_importances').table.data.length,2);
 assert.equal(latest('feature_importances').spec.title,'Ranked features');
 assert.equal(latest('elbow_curve').spec.vconcat,undefined);
 assert(run.history.some(row=>row.elbow_curve?.spec?.vconcat));
 assert.deepEqual(new Set(latest('silhouette_plot').table.data.map(row=>row[2])),new Set(['A','B','C']));
 assert(run.history.some(row=>row.roc?.table.data.some(value=>value[0]==='micro-average')));
 assert(!latest('roc').table.data.some(value=>value[0]==='micro-average'));
 assert(!latest('precision_recall').table.data.some(value=>value[0]==='micro-average'));
 for(const key of ['calibration_curve','silhouette_plot','confusion_matrix','roc','precision_recall','feature_importances']){
  const chart=run.history.find(row=>row[key])?.[key];assert(chart,key);assert(chart.table.data.length>0,key);
 }
});
