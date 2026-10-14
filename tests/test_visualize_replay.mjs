/** Public chart values retain their native table/spec snapshots in the UI. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents,flatten,keyPath} from '../web/model.mjs';
const columns=row=>Object.fromEntries(flatten(row).map(([key,value])=>[keyPath(key).join('.'),value]).filter(([key])=>!['_timestamp','_runtime'].includes(key)));
test('legacy chart snapshots agree after edits, delayed visibility and shared writer replay',()=>{
 const snapshots=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/visualize_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 for(const snapshot of snapshots){
  const replay=normalizeEvents([...snapshot.events].reverse().concat(snapshot.events));
  assert.deepEqual(replay.issues,[]);assert.equal(replay.runs.length,1);
  const run=replay.runs[0];assert.equal(run.state,snapshot.expected.state);
  assert.deepEqual(run.history.map(columns),snapshot.expected.history.map(columns));
  const importance=run.history.filter(row=>row.importance).map(row=>row.importance);
  assert.deepEqual(importance.map(chart=>chart.table.data.length),[2,3]);
  assert.deepEqual(importance.map(chart=>chart.spec.mark),['bar','bar']);
  if(run.state==='finished'){
   assert.equal(new Set(run.history.map(row=>row._writer)).size,2);
   assert.equal(run.history.find(row=>row.confusion).confusion.kind,'confusion_matrix');
   assert.equal(run.history.find(row=>row.registered).registered.spec.mark,'line');
   assert.deepEqual(run.history.find(row=>row.registered).registered.table.data,[[0,3],[1,2],[2,1]]);
  }
 }
});
